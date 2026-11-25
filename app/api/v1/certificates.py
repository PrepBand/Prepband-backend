"""Certificates (FR-CERT-01..08): the student's own list, details, consent
toggle and on-demand PDF; a minimal public verification lookup; and admin
revocation."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlmodel import Session, select

from ...core.config import Settings, get_settings
from ...core.roles import require_admin
from ...core.security import get_current_user_id
from ...db.session import get_session
from ...models import Certificate, MockAttempt, Profile
from ...schemas.mocks import (
    AdminCertificateOut,
    CertificateDetail,
    CertificateOut,
    CertificateRevoke,
    CertificateUpdate,
    VerificationOut,
)
from ...services.audit import audit_note
from ...services.certificate_pdf import render_certificate_pdf
from ...services.mock_flow import holder_name, module_results, normalize_certificate_code, overall_band
from ...services.rate_limit import certificate_pdf_limiter, client_ip, verification_limiter

router = APIRouter()


def _verification_url(certificate: Certificate, settings: Settings) -> str | None:
    if not certificate.public_verification or certificate.revoked_at is not None:
        return None
    return f"{settings.frontend_url.rstrip('/')}/verify/{certificate.code}"


def _require_owned_certificate(session: Session, certificate_id: UUID, student_id: UUID) -> Certificate:
    certificate = session.get(Certificate, certificate_id)
    if certificate is None or certificate.student_id != student_id:
        raise HTTPException(status_code=404, detail="Certificate not found.")
    return certificate


def _to_certificate_out(session: Session, certificate: Certificate, settings: Settings) -> CertificateOut:
    mock = session.get(MockAttempt, certificate.mock_attempt_id)
    return CertificateOut(
        id=str(certificate.id),
        code=certificate.code,
        issued_at=certificate.issued_at,
        revoked_at=certificate.revoked_at,
        revoked_reason=certificate.revoked_reason,
        public_verification=certificate.public_verification,
        mock_attempt_id=str(certificate.mock_attempt_id),
        track=mock.track,
        completed_at=mock.completed_at,
        verification_url=_verification_url(certificate, settings),
    )


@router.get("/certificates", response_model=list[CertificateOut])
def list_certificates(
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[CertificateOut]:
    rows = session.exec(
        select(Certificate).where(Certificate.student_id == UUID(user_id)).order_by(Certificate.issued_at.desc())
    ).all()
    return [_to_certificate_out(session, row, settings) for row in rows]


@router.get("/certificates/{certificate_id}", response_model=CertificateDetail)
def get_certificate(
    certificate_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CertificateDetail:
    certificate = _require_owned_certificate(session, certificate_id, UUID(user_id))
    mock = session.get(MockAttempt, certificate.mock_attempt_id)
    results = module_results(session, mock)
    band, note = overall_band(results)
    base = _to_certificate_out(session, certificate, settings)
    return CertificateDetail(
        **base.model_dump(),
        holder_name=holder_name(session, certificate.student_id),
        modules=results,
        overall_band=band,
        overall_band_note=note,
    )


@router.patch("/certificates/{certificate_id}", response_model=CertificateOut)
def update_certificate(
    certificate_id: UUID,
    body: CertificateUpdate,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CertificateOut:
    """FR-CERT-06: the holder opts in or out of public verification.
    Nothing else about a certificate can be changed by its holder."""

    certificate = _require_owned_certificate(session, certificate_id, UUID(user_id))
    if body.public_verification and certificate.revoked_at is not None:
        raise HTTPException(status_code=409, detail="A revoked certificate can't be made publicly verifiable.")
    certificate.public_verification = body.public_verification
    certificate.updated_at = datetime.now(timezone.utc)
    session.add(certificate)
    session.commit()
    session.refresh(certificate)
    return _to_certificate_out(session, certificate, settings)


@router.get("/certificates/{certificate_id}/pdf")
def download_certificate_pdf(
    certificate_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """FR-CERT-05/07: generated on demand from the server's own records and
    streamed back; never stored, never built from client-supplied data."""

    certificate_pdf_limiter.check(user_id)
    certificate = _require_owned_certificate(session, certificate_id, UUID(user_id))
    mock = session.get(MockAttempt, certificate.mock_attempt_id)
    if mock is None or mock.status != "completed":
        raise HTTPException(status_code=409, detail="This certificate's mock test is not complete.")
    results = module_results(session, mock)
    band, note = overall_band(results)
    pdf = render_certificate_pdf(
        code=certificate.code,
        holder_name=holder_name(session, certificate.student_id),
        track=mock.track,
        issued_at=certificate.issued_at,
        completed_at=mock.completed_at,
        modules=results,
        overall_band=band,
        overall_band_note=note,
        verification_url=_verification_url(certificate, settings),
        revoked_at=certificate.revoked_at,
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="prepband-certificate-{certificate.code}.pdf"',
            "Cache-Control": "no-store",
        },
    )


# --- public verification (no auth) -------------------------------------------


@router.get("/verify/{code}", response_model=VerificationOut)
def verify_certificate(
    code: str,
    request: Request,
    session: Session = Depends(get_session),
) -> VerificationOut:
    """FR-CERT-06: minimal public lookup - ID, validity, issue date. A
    certificate whose holder hasn't opted in looks exactly like one that
    doesn't exist, so codes can't be probed."""

    verification_limiter.check(client_ip(request))
    certificate = session.exec(select(Certificate).where(Certificate.code == normalize_certificate_code(code))).first()
    if certificate is None or not certificate.public_verification:
        raise HTTPException(status_code=404, detail="No publicly verifiable certificate has this ID.")
    revoked = certificate.revoked_at is not None
    return VerificationOut(
        code=certificate.code,
        status="revoked" if revoked else "valid",
        issued_on=certificate.issued_at.date(),
        statement=(
            "This certificate was revoked and is no longer valid."
            if revoked
            else "This is a genuine Prepband practice completion certificate. It is unofficial and is not an IELTS result."
        ),
    )


# --- admin (FR-CERT-08) ----------------------------------------------------------


def _to_admin_out(session: Session, certificate: Certificate, settings: Settings) -> AdminCertificateOut:
    profile = session.get(Profile, certificate.student_id)
    base = _to_certificate_out(session, certificate, settings)
    return AdminCertificateOut(
        **base.model_dump(),
        student_id=str(certificate.student_id),
        holder_name=holder_name(session, certificate.student_id),
        holder_email=profile.email if profile else None,
    )


@router.get("/admin/certificates", response_model=list[AdminCertificateOut])
def admin_list_certificates(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[AdminCertificateOut]:
    rows = session.exec(select(Certificate).order_by(Certificate.issued_at.desc()).limit(500)).all()
    return [_to_admin_out(session, row, settings) for row in rows]


@router.post("/admin/certificates/{certificate_id}/revoke", response_model=AdminCertificateOut)
def admin_revoke_certificate(
    certificate_id: UUID,
    body: CertificateRevoke,
    request: Request,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AdminCertificateOut:
    certificate = session.get(Certificate, certificate_id)
    if certificate is None:
        raise HTTPException(status_code=404, detail="Certificate not found.")
    if certificate.revoked_at is None:
        now = datetime.now(timezone.utc)
        certificate.revoked_at = now
        certificate.revoked_reason = body.reason.strip()
        audit_note(request, reason=certificate.revoked_reason, code=certificate.code)
        certificate.updated_at = now
        session.add(certificate)
        session.commit()
        session.refresh(certificate)
    return _to_admin_out(session, certificate, settings)
