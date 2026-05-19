#!/usr/bin/env nix-shell
#!nix-shell -i python3 -p python3Packages.fonttools

from argparse import ArgumentParser, Namespace
from pathlib import Path
import re
import shutil
import unicodedata

from fontTools.ttLib import TTCollection, TTFont
from fontTools.ttLib.tables._n_a_m_e import NameRecord


STATUS_OK = "OK"
STATUS_DRY = "DRY"
STATUS_RENAMED = "RENAMED"
STATUS_FAILED = "FAILED"

UNKNOWN_FONT_NAME = "Unknown-Font"
COLLECTION_LABEL = "Collection"
FONTS_LABEL = "fonts"

SUPPORTED_EXTENSIONS = {
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".ttc",
    ".otc",
}

COLLECTION_EXTENSIONS = {
    ".ttc",
    ".otc",
}

NAME_ID_FAMILY = 1
NAME_ID_STYLE = 2
NAME_ID_FULL = 4
NAME_ID_POSTSCRIPT = 6

MAX_FILENAME_LENGTH = 180
MAX_NON_ASCII_TOKEN_RATIO = 0.40

BRACKETED_TEXT_PATTERN = r"\s*[\[\(\{<]([^\]\)\}>]*)[\]\)\}>]\s*"
WHITESPACE_PATTERN = r"\s+"
TOKEN_UNSAFE_CHARS_PATTERN = r"[^\w.+]+"
ASCII_UNSAFE_CHARS_PATTERN = r"[^A-Za-z0-9_.+]+"

IGNORED_BRACKET_VALUES = {
    "ttf",
    "otf",
    "ttc",
    "otc",
    "truetype",
    "opentype",
    "font",
    "fonts",
}

NAME_RECORD_ENCODINGS = (
    "utf-16-be",
    "utf-8",
    "cp1252",
    "latin-1",
    "mac_roman",
)


def parse_args() -> Namespace:
    # Parse command line arguments
    parser = ArgumentParser(
        description="Rename font files using their internal font metadata names",
    )

    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="Directory to scan, defaults to the current directory",
    )

    parser.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="Actually rename files instead of only showing a dry run",
    )

    parser.add_argument(
        "-n",
        "--no-recursive",
        action="store_true",
        help="Only scan the given directory, not subdirectories",
    )

    return parser.parse_args()


def has_diacritic(character: str) -> bool:
    # Detect composed and decomposed accents generically
    return any(
        unicodedata.category(part) == "Mn"
        for part in unicodedata.normalize("NFD", character)
    )


def score_text(text: str) -> int:
    # Score decoded metadata so broken encodings lose against readable names
    score = 0

    for index, character in enumerate(text):
        category = unicodedata.category(character)

        if character == "\ufffd" or category.startswith("C"):
            score -= 100
            continue

        if character.isalnum():
            score += 5
        elif character in " .+-_":
            score += 3
        else:
            score -= 2

        previous_character = text[index - 1] if index > 0 else ""
        next_character = text[index + 1] if index + 1 < len(text) else ""

        if (
            character.isupper()
            and has_diacritic(character)
            and previous_character.islower()
            and not next_character.isupper()
        ):
            score -= 8

    return score


def normalize_text(text: str) -> str:
    # Normalize Unicode and remove null padding
    text = text.replace("\x00", "")
    text = unicodedata.normalize("NFC", text)

    return text.strip()


def decode_name_record(record: NameRecord) -> str:
    # Decode a font name record using declared and fallback encodings
    candidates = []

    try:
        candidates.append(record.toUnicode())
    except Exception:
        pass

    for encoding in NAME_RECORD_ENCODINGS:
        try:
            candidates.append(record.string.decode(encoding))
        except Exception:
            pass

    candidates = [
        normalize_text(candidate)
        for candidate in candidates
        if normalize_text(candidate)
    ]

    if not candidates:
        return ""

    return max(candidates, key=score_text)


def get_name_record_priority(record: NameRecord) -> int:
    # Prefer modern Unicode name records when scores are close
    if record.platformID == 3:
        return 30

    if record.platformID == 0:
        return 20

    if record.platformID == 1:
        return 10

    return 0


def get_name_value(font: TTFont, name_id: int) -> str | None:
    # Get the best decoded value for a name table ID
    records = [
        record
        for record in font["name"].names
        if record.nameID == name_id
    ]

    if not records:
        return None

    scored_names = []

    for record in records:
        name = decode_name_record(record)

        if not name:
            continue

        score = score_text(name) + get_name_record_priority(record)
        scored_names.append((score, name))

    if not scored_names:
        return None

    return max(scored_names)[1]


def clean_bracketed_text(match: re.Match[str]) -> str:
    # Drop junk bracket labels but keep useful numeric variants
    value = match.group(1).strip()
    normalized_value = value.lower().strip()

    if normalized_value in IGNORED_BRACKET_VALUES:
        return " "

    if value.isdigit():
        return f" {value} "

    if re.fullmatch(r"cp\d+", normalized_value):
        return f" {value.upper()} "

    return " "


def get_non_ascii_ratio(text: str) -> float:
    # Measure how much of a token is non ASCII
    if not text:
        return 0.0

    non_ascii_count = sum(ord(character) > 127 for character in text)

    return non_ascii_count / len(text)


def is_latin_non_ascii_token(token: str) -> bool:
    # Check whether every non ASCII letter is from the Latin script
    for character in token:
        if ord(character) <= 127:
            continue

        if not character.isalpha():
            return False

        if "LATIN" not in unicodedata.name(character, ""):
            return False

    return True


def fold_token_to_ascii(token: str) -> str:
    # Convert light Latin accents to plain ASCII
    token = unicodedata.normalize("NFKD", token)
    token = token.encode("ascii", "ignore").decode("ascii")
    token = re.sub(ASCII_UNSAFE_CHARS_PATTERN, "", token)

    return token


def clean_token(token: str) -> str | None:
    # Clean one filename section and drop bad Unicode sections
    token = re.sub(TOKEN_UNSAFE_CHARS_PATTERN, "", token, flags=re.UNICODE)

    if not token:
        return None

    if token.isascii():
        return token

    if (
        is_latin_non_ascii_token(token)
        and get_non_ascii_ratio(token) <= MAX_NON_ASCII_TOKEN_RATIO
    ):
        folded_token = fold_token_to_ascii(token)

        if folded_token:
            return folded_token

    return None


def split_name_tokens(name: str) -> list[str]:
    # Split on separators before Unicode cleanup
    name = re.sub(BRACKETED_TEXT_PATTERN, clean_bracketed_text, name)
    name = name.translate(str.maketrans("", "", "()[]{}<>"))
    name = re.sub(WHITESPACE_PATTERN, " ", name).strip()

    return [
        token
        for token in re.split(r"[\s\-_]+", name)
        if token
    ]


def clean_filename(name: str) -> str:
    # Normalize the raw metadata name into a safe ASCII filename
    name = normalize_text(name)

    cleaned_tokens = []

    for token in split_name_tokens(name):
        cleaned_token = clean_token(token)

        if cleaned_token:
            cleaned_tokens.append(cleaned_token)

    filename = "-".join(cleaned_tokens)

    return filename[:MAX_FILENAME_LENGTH] or UNKNOWN_FONT_NAME


def get_font_name(font: TTFont) -> str:
    # Read the preferred display name from the font name table
    full_name = get_name_value(font, NAME_ID_FULL)
    family_name = get_name_value(font, NAME_ID_FAMILY)
    style_name = get_name_value(font, NAME_ID_STYLE)
    postscript_name = get_name_value(font, NAME_ID_POSTSCRIPT)

    if full_name:
        return full_name

    if family_name and style_name:
        return f"{family_name} {style_name}"

    if family_name:
        return family_name

    if postscript_name:
        return postscript_name

    return UNKNOWN_FONT_NAME


def get_metadata_name(path: Path) -> str:
    # Read the metadata name from a font file or collection
    extension = path.suffix.lower()

    if extension in COLLECTION_EXTENSIONS:
        collection = TTCollection(path)
        font_names = [get_font_name(font) for font in collection.fonts]

        return f"{font_names[0]} {COLLECTION_LABEL} {len(font_names)} {FONTS_LABEL}"

    font = TTFont(path, lazy=True)

    return get_font_name(font)


def get_numbered_target(source: Path, target: Path) -> Path:
    # Add incrementing numbers when the cleaned target already exists
    if target == source or not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    index = 1

    while True:
        numbered_target = target.with_name(f"{stem}-{index}{suffix}")

        if numbered_target == source or not numbered_target.exists():
            return numbered_target

        index += 1


def iter_font_paths(directory: Path, recursive: bool) -> list[Path]:
    # Find supported font files in the selected directory
    paths = directory.rglob("*") if recursive else directory.iterdir()

    return sorted(
        path
        for path in paths
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def print_status(status: str, message: str) -> None:
    # Print aligned status output
    print(f"{status:<8} {message}")


def rename_font(path: Path, apply_changes: bool) -> None:
    # Rename one font file from its internal metadata name
    metadata_name = get_metadata_name(path)
    filename = clean_filename(metadata_name)
    extension = path.suffix.lower()
    base_target = path.with_name(f"{filename}{extension}")
    target = get_numbered_target(path, base_target)

    if path.name == target.name:
        print_status(STATUS_OK, str(path))
        return

    rename_message = f"{path} -> {target}"

    if not apply_changes:
        print_status(STATUS_DRY, rename_message)
        return

    shutil.move(str(path), str(target))
    print_status(STATUS_RENAMED, rename_message)


def main() -> None:
    # Run the font rename workflow
    args = parse_args()
    directory = args.directory.expanduser().resolve()
    recursive = not args.no_recursive

    for path in iter_font_paths(directory, recursive):
        try:
            rename_font(path, args.apply)

        except Exception as error:
            print_status(STATUS_FAILED, f"{path}: {error}")


if __name__ == "__main__":
    main()