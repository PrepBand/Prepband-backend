// upload-test-media: admin-only upload of Listening audio (and test
// images) into the existing `prepband-avatars` R2 bucket, under the
// prefixes reserved by database/migrations/0001_content_model.sql:
//   test-audio/<test_version_id>/<uuid>.<ext>
//   test-images/<test_version_id>/<uuid>.<ext>
//
// Same shape as the deployed `upload-avatar` function (verify_jwt: true,
// aws4fetch-signed PUT, same 5 R2_* secrets - no new secret needed), plus
// two checks avatar uploads don't need:
//   1. the caller is an admin (public.is_admin(), evaluated as the caller)
//   2. the target test_version is still a draft (published content is
//      immutable, so a file can never be attached to it after the fact)
//
// Phase 5 adds one more target, purpose=examiner_phrase: a voice clip for a
// line in the live examiner's phrase bank (examiner_phrases). Those lines
// aren't part of any test version, so there's no draft check; it is still
// admin-only and audio-only, stored under examiner-audio/<uuid>.<ext>.
//
// It only stores the file and returns its metadata. The admin UI then
// records it on a media_assets row through the backend
// (PATCH /admin/media-assets/{id} or POST /admin/sections/{id}/media-assets),
// so all content writes keep going through the one API that validates them.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";
import { AwsClient } from "https://esm.sh/aws4fetch@1.0.20";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const AUDIO_TYPES: Record<string, string> = {
  "audio/mpeg": "mp3",
  "audio/mp3": "mp3",
  "audio/wav": "wav",
  "audio/x-wav": "wav",
  "audio/wave": "wav",
  "audio/ogg": "ogg",
  "audio/webm": "webm",
  "audio/mp4": "m4a",
  "audio/x-m4a": "m4a",
  "audio/aac": "aac",
};
const IMAGE_TYPES: Record<string, string> = {
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/webp": "webp",
};
const MAX_AUDIO_BYTES = 40 * 1024 * 1024;
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
  });
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: CORS_HEADERS });
  }
  if (req.method !== "POST") {
    return json({ error: "Method not allowed" }, 405);
  }

  const authHeader = req.headers.get("Authorization");
  if (!authHeader) {
    return json({ error: "Missing Authorization header" }, 401);
  }

  const supabase = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_ANON_KEY")!, {
    global: { headers: { Authorization: authHeader } },
  });

  const { data: userData, error: userError } = await supabase.auth.getUser();
  if (userError || !userData.user) {
    return json({ error: "Not authenticated" }, 401);
  }

  const { data: isAdmin, error: adminError } = await supabase.rpc("is_admin");
  if (adminError) {
    console.error("is_admin check failed", adminError);
    return json({ error: "Could not verify admin role." }, 500);
  }
  if (isAdmin !== true) {
    return json({ error: "Admin role required." }, 403);
  }

  let form: FormData;
  try {
    form = await req.formData();
  } catch {
    return json({ error: "Expected multipart/form-data with 'file' and 'test_version_id' fields" }, 400);
  }

  const file = form.get("file");
  const versionId = String(form.get("test_version_id") ?? "");
  const purpose = String(form.get("purpose") ?? "test_media");
  if (!(file instanceof File)) {
    return json({ error: "Missing 'file' field" }, 400);
  }
  if (purpose !== "test_media" && purpose !== "examiner_phrase") {
    return json({ error: "Unknown 'purpose'" }, 400);
  }
  const isPhrase = purpose === "examiner_phrase";
  if (!isPhrase && !UUID_RE.test(versionId)) {
    return json({ error: "Missing or invalid 'test_version_id'" }, 400);
  }

  const isAudio = file.type in AUDIO_TYPES;
  const isImage = file.type in IMAGE_TYPES;
  if (!isAudio && !isImage) {
    return json({ error: "Unsupported file type. Use MP3, WAV, OGG, WebM, M4A or AAC audio, or a JPEG/PNG/WebP image." }, 400);
  }
  if (isAudio && file.size > MAX_AUDIO_BYTES) {
    return json({ error: "Audio must be 40MB or smaller." }, 400);
  }
  if (isImage && file.size > MAX_IMAGE_BYTES) {
    return json({ error: "Images must be 5MB or smaller." }, 400);
  }
  if (isPhrase && !isAudio) {
    return json({ error: "Examiner lines take audio only." }, 400);
  }

  // RLS lets admins read every version, so a missing row really is missing.
  const { data: version, error: versionError } = isPhrase
    ? { data: { id: null, status: "draft" }, error: null }
    : await supabase
        .from("test_versions")
        .select("id, status")
        .eq("id", versionId)
        .maybeSingle();
  if (versionError) {
    console.error("test_versions lookup failed", versionError);
    return json({ error: "Could not look up the test version." }, 500);
  }
  if (!version) {
    return json({ error: "Test version not found." }, 404);
  }
  if (version.status !== "draft") {
    return json({ error: "Only a draft test can receive new media." }, 409);
  }

  const accountId = Deno.env.get("R2_ACCOUNT_ID");
  const bucket = Deno.env.get("R2_BUCKET_NAME");
  const accessKeyId = Deno.env.get("R2_ACCESS_KEY_ID");
  const secretAccessKey = Deno.env.get("R2_SECRET_ACCESS_KEY");
  const publicBaseUrl = Deno.env.get("R2_PUBLIC_BASE_URL");
  if (!accountId || !bucket || !accessKeyId || !secretAccessKey || !publicBaseUrl) {
    console.error("R2 secrets are not fully configured");
    return json({ error: "Media storage is not configured yet (R2 secrets missing in Supabase)." }, 500);
  }

  const ext = isAudio ? AUDIO_TYPES[file.type] : IMAGE_TYPES[file.type];
  const key = isPhrase
    ? `examiner-audio/${crypto.randomUUID()}.${ext}`
    : `${isAudio ? "test-audio" : "test-images"}/${versionId}/${crypto.randomUUID()}.${ext}`;

  const r2 = new AwsClient({ accessKeyId, secretAccessKey, service: "s3", region: "auto" });
  const putResponse = await r2.fetch(`https://${accountId}.r2.cloudflarestorage.com/${bucket}/${key}`, {
    method: "PUT",
    headers: { "Content-Type": file.type },
    body: await file.arrayBuffer(),
  });

  if (!putResponse.ok) {
    console.error("R2 upload failed", putResponse.status, await putResponse.text());
    return json({ error: "Upload to storage failed." }, 502);
  }

  return json({
    storage_key: key,
    public_url: `${publicBaseUrl.replace(/\/$/, "")}/${key}`,
    content_type: file.type,
    size_bytes: file.size,
    type: isAudio ? "audio" : "image",
  });
});
