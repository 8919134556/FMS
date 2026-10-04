import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator

mobile_number_validator = RegexValidator(
    regex=r"^\+?[0-9]{7,15}$",
    message="Enter a valid mobile number (7-15 digits, optional leading +).",
)

pincode_validator = RegexValidator(
    regex=r"^[0-9A-Za-z\- ]{4,10}$",
    message="Enter a valid postal/pin code.",
)

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".jpg", ".jpeg", ".png",
}

# Defense in depth: rejected outright even if somehow present in an allow-list
# above (extension-based checks alone are trivially spoofed, so business
# documents get both an allow-list AND this explicit deny-list).
DANGEROUS_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".sh", ".ps1", ".js", ".php", ".py",
    ".msi", ".com", ".scr", ".jar", ".vbs", ".dll",
}

# First bytes ("magic numbers") of the file formats FMS documents actually
# accept — a cheap content sniff that doesn't need a third-party library
# (python-magic needs a native libmagic install that's awkward on Windows).
# This catches "renamed .exe to .pdf" without claiming full MIME detection.
_MAGIC_SIGNATURES = {
    b"%PDF": ".pdf",
    b"\x89PNG": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"PK\x03\x04": ".docx",  # also .xlsx/.csv-as-zip — modern Office formats are zip containers
    b"\xd0\xcf\x11\xe0": ".doc",  # legacy OLE2 container — also covers .xls
}


def validate_file_size(file):
    max_mb = getattr(settings, "MAX_UPLOAD_SIZE_MB", 5)
    if file.size > max_mb * 1024 * 1024:
        raise ValidationError(f"File size must not exceed {max_mb} MB.")


def validate_image_extension(file):
    ext = os.path.splitext(file.name)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file type '{ext}'. Allowed types: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}."
        )


def validate_document_extension(file):
    ext = os.path.splitext(file.name)[1].lower()
    if ext in DANGEROUS_EXTENSIONS:
        raise ValidationError("Executable and script files are not allowed.")
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file type '{ext}'. Allowed types: {', '.join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))}."
        )


def validate_document_content(file):
    """Best-effort magic-byte sniff — flags files whose actual content
    clearly isn't any business-document format FMS accepts (e.g. a binary
    renamed to .pdf). Not a substitute for the extension checks above;
    a genuine but unrecognized format is allowed through rather than
    guessed at, since this only ever rejects, never approves."""
    try:
        file.seek(0)
        header = file.read(8)
    finally:
        file.seek(0)
    if not header:
        return
    if any(header.startswith(sig) for sig in _MAGIC_SIGNATURES):
        return
    # Plain text formats (.csv, and older .doc/.xls variants some tools
    # emit as text) have no reliable magic number — only reject when the
    # header looks like a compiled/executable binary, not merely "unknown".
    if header[:2] in (b"MZ",) or header[:4] in (b"\x7fELF",):
        raise ValidationError("This file's content does not match an allowed document type.")
