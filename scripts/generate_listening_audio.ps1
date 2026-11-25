# Generates the Listening seed audio from the transcripts in
# database/seeds/*.json, using the free text-to-speech voices built into
# Windows (System.Speech: "Microsoft David" and "Microsoft Zira"). Zero
# cost and fully original - the audio is synthesized from Prepband's own
# scripts, which is why each seed's attribution says "synthesized speech".
#
#   powershell -ExecutionPolicy Bypass -File scripts\generate_listening_audio.ps1
#
# Output: database/seeds/audio/<name>.wav (16 kHz, 16-bit mono - small
# enough for the upload-test-media Edge Function's 40MB limit). Files are
# gitignored; upload them through Admin > Tests > (test) > Upload audio.
#
# Each transcript line is "SPEAKER: text". Voices are chosen per file
# below, because the same label (e.g. CALLER) is a woman in one script and
# a man in another.

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech

$root = Split-Path -Parent $PSScriptRoot
$seedsDir = Join-Path $root "database\seeds"
$outDir = Join-Path $seedsDir "audio"
New-Item -ItemType Directory -Force $outDir | Out-Null

$male = "Microsoft David Desktop"
$female = "Microsoft Zira Desktop"

$voiceMap = @{
  "listening-mock-1-part-1.wav" = @{ NARRATOR = $male; AGENT = $male; CALLER = $female }
  "listening-mock-1-part-2.wav" = @{ NARRATOR = $male; GUIDE = $female }
  "listening-mock-1-part-3.wav" = @{ NARRATOR = $male; PRIYA = $female; DANIEL = $male }
  "listening-mock-1-part-4.wav" = @{ NARRATOR = $female; LECTURER = $male }
  "listening-practice-1.wav"    = @{ NARRATOR = $male; AGENT = $female; CALLER = $male }
}

$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)

foreach ($file in Get-ChildItem $seedsDir -Filter *.json) {
  $doc = Get-Content $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($section in $doc.sections) {
    foreach ($media in @($section.media_assets)) {
      if ($null -eq $media -or -not $media.local_file) { continue }
      $name = Split-Path -Leaf $media.local_file
      $voices = $voiceMap[$name]
      if ($null -eq $voices) { throw "No voice mapping for $name - add it to `$voiceMap." }

      $prompt = New-Object System.Speech.Synthesis.PromptBuilder
      $prompt.AppendBreak([TimeSpan]::FromMilliseconds(800))
      foreach ($line in $media.transcript) {
        if ($line -notmatch '^([A-Z]+):\s*(.+)$') { throw "Transcript line without a SPEAKER: label in $name : $line" }
        $speaker = $Matches[1]
        $text = $Matches[2]
        $voice = $voices[$speaker]
        if (-not $voice) { throw "No voice for speaker $speaker in $name" }
        $prompt.StartVoice($voice)
        $prompt.AppendText($text)
        $prompt.EndVoice()
        # Narrator instructions get the longer pause, like a real test.
        $pause = if ($speaker -eq "NARRATOR") { 2500 } else { 600 }
        $prompt.AppendBreak([TimeSpan]::FromMilliseconds($pause))
      }

      $outPath = Join-Path $outDir $name
      $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
      $synth.Rate = -1
      $synth.SetOutputToWaveFile($outPath, $format)
      $synth.Speak($prompt)
      $synth.Dispose()

      $sizeMb = [math]::Round((Get-Item $outPath).Length / 1MB, 1)
      $seconds = [math]::Round(((Get-Item $outPath).Length - 44) / 32000)
      Write-Output ("{0}  {1} MB  ~{2}s" -f $name, $sizeMb, $seconds)
    }
  }
}
