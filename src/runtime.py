"""Generate a local lock runtime from installed Omarchy sources.

Compatibility is structural, not a hard-coded Omarchy version: an unknown or
ambiguous edit site raises ValueError before any output is created.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import tempfile


_VIEW_EDITS = (
    ("  property bool loadBackground: true\n",
     "  property bool loadBackground: true\n"
     "  property bool previewSaver: false\n"
     "  property bool saverDismissed: false\n"),
    ("  property bool syncingPasswordText: false\n",
     "  property bool syncingPasswordText: false\n"
     "  readonly property bool saverActive: loadBackground && (previewSaver ||\n"
     "    (inputEnabled && !saverDismissed && passwordText.length === 0 &&\n"
     "     failureMessage.length === 0 && !authenticatingPassword))\n"),
    ("  onPasswordTextChanged: syncPasswordText()\n",
     "  function dismissSaver() {\n"
     "    if (!inputEnabled || saverDismissed) return\n"
     "    saverDismissed = true\n"
     "    wakeRequested()\n"
     "    Qt.callLater(forcePasswordFocus)\n"
     "  }\n\n"
     "  onPasswordTextChanged: {\n"
     "    syncPasswordText()\n"
     "    if (passwordText.length > 0) dismissSaver()\n"
     "  }\n"),
    ("  onInputEnabledChanged: {\n"
     "    if (inputEnabled) Qt.callLater(forcePasswordFocus)\n  }",
     "  onInputEnabledChanged: {\n"
     "    if (inputEnabled) {\n"
     "      saverDismissed = false\n"
     "      Qt.callLater(forcePasswordFocus)\n"
     "    }\n  }"),
    ("      id: inputField\n",
     "      id: inputField\n      opacity: root.saverActive ? 0 : 1\n"),
    ("        Keys.onPressed: function(event) {\n",
     "        Keys.onPressed: function(event) {\n          root.dismissSaver()\n"),
)
_PREVIEW = "      inputEnabled: false\n      loadBackground: root.previewVisible\n"
_SAVER = ("\n    SecureSaver {\n"
          "      anchors.fill: parent\n"
          "      active: root.saverActive\n"
          "      onDismissed: root.dismissSaver()\n"
          "    }\n")


_PROMPT_TIMEOUT = '''
  // Service activity is shared across all outputs. A second screen must not
  // clear a password while the user is active on another screen.
  property int promptActivitySerial: 0
  onPromptActivitySerialChanged: {
    if (inputEnabled && saverDismissed && !authenticatingPassword) promptIdleTimer.restart()
  }

  Timer {
    id: promptIdleTimer
    interval: 30000
    repeat: false
    running: root.inputEnabled && root.saverDismissed && !root.authenticatingPassword
    onTriggered: {
      if (!root.inputEnabled || root.authenticatingPassword) return
      root.passwordTextEdited("")
      root.clearFailureRequested()
      root.saverDismissed = false
    }
  }

'''


def _require_one(text: str, marker: str, label: str) -> None:
    if text.count(marker) != 1:
        raise ValueError(f"Unsupported stock {label}: missing or ambiguous {marker.strip()!r}")


def _code_mask(source: str) -> str:
    """Hide strings/comments without moving offsets; reject unfamiliar quoting."""
    result = list(source)
    index = 0
    while index < len(source):
        start = index
        if source.startswith("//", index):
            end = source.find("\n", index)
            index = len(source) if end < 0 else end
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise ValueError("Unsupported stock: unterminated comment")
            index = end + 2
        elif source[index] in ('"', "'"):
            quote = source[index]
            index += 1
            while index < len(source) and source[index] != quote:
                index += 2 if source[index] == "\\" else 1
            if index >= len(source):
                raise ValueError("Unsupported stock: unterminated string")
            index += 1
        elif source[index] == "`":
            raise ValueError("Unsupported stock: template-string syntax needs review")
        else:
            index += 1
            continue
        for position in range(start, index):
            if source[position] != "\n":
                result[position] = " "
    return "".join(result)


class _Structure:
    """Small fail-closed brace/scope guard, not a general QML parser."""

    def __init__(self, source: str):
        self.source = source
        self.code = _code_mask(source)
        self.parents: dict[int, int | None] = {}
        self.ends: dict[int, int] = {}
        stack: list[int] = []
        for position, char in enumerate(self.code):
            if char == "{":
                self.parents[position] = stack[-1] if stack else None
                stack.append(position)
            elif char == "}":
                if not stack:
                    raise ValueError("Unsupported stock: unbalanced braces")
                self.ends[stack.pop()] = position
        if stack:
            raise ValueError("Unsupported stock: unbalanced braces")
        self.root = self.object_id("root", "Item", None)
        if sum(parent is None for parent in self.parents.values()) != 1:
            raise ValueError("Unsupported stock: multiple root objects")
        if self.code[self.ends[self.root] + 1:].strip():
            raise ValueError("Unsupported stock: content after root object")

    def owner(self, position: int) -> int | None:
        return max((start for start, end in self.ends.items()
                    if start < position < end), default=None)

    def require_type(self, start: int, kind: str) -> None:
        if not re.search(r"\b" + re.escape(kind) + r"\s*\{$", self.code[:start + 1]):
            raise ValueError(f"Unsupported stock: expected {kind} object")

    def object_id(self, identifier: str, kind: str, parent: int | None) -> int:
        matches = list(re.finditer(r"\bid\s*:\s*" + re.escape(identifier) + r"\b", self.code))
        if len(matches) != 1:
            raise ValueError(f"Unsupported stock: missing or ambiguous id {identifier}")
        owner = self.owner(matches[0].start())
        if owner is None or self.parents[owner] != parent:
            raise ValueError(f"Unsupported stock: unexpected parent of {identifier}")
        self.require_type(owner, kind)
        return owner

    def member_count(self, name: str, parent: int) -> int:
        return sum(self.owner(match.start()) == parent for match in re.finditer(
            r"\b" + re.escape(name) + r"\s*:", self.code))

    def at(self, marker: str, parent: int) -> None:
        _require_one(self.source, marker, "QML")
        position = self.source.index(marker)
        if self.code[position:position + len(marker)] != marker:
            raise ValueError("Unsupported stock: marker inside string or comment")
        if self.owner(position + len(marker) - len(marker.lstrip())) != parent:
            raise ValueError("Unsupported stock: marker has unexpected scope")


def _patch_service(source: str, blank_seconds: int) -> tuple[str, str]:
    structure = _Structure(source)
    timer = structure.object_id("idleBlankTimer", "Timer", structure.root)
    if structure.member_count("interval", timer) != 1 or structure.member_count("idleMs", timer) > 1:
        raise ValueError("Unsupported stock: ambiguous idle timer properties")
    session = structure.object_id("sessionLock", "WlSessionLock", structure.root)
    surface = structure.object_id("lockSurface", "WlSessionLockSurface", session)
    lock_view = structure.object_id("lockView", "LockView", surface)
    structure.at("  function runWake() {\n", structure.root)
    structure.at("        passwordText: root.enteredPassword\n", lock_view)
    structure.object_id("passwordPam", "PamContext", structure.root)
    structure.at("    id: idleBlankTimer\n", timer)
    preview_window = structure.object_id("previewWindow", "PanelWindow", structure.root)
    _require_one(source, _PREVIEW, "Service.qml preview")
    preview = structure.owner(source.index(_PREVIEW) + 6)
    if preview is None or structure.parents[preview] != preview_window:
        raise ValueError("Unsupported stock: preview LockView scope changed")
    structure.require_type(preview, "LockView")
    structure.at(_PREVIEW, preview)
    body = source[timer:structure.ends[timer]]
    if structure.member_count("idleMs", timer):
        matches = list(re.finditer(
            r"(?m)^    readonly property int idleMs: ([0-9]+)(?=[ \t]*(?://[^\n]*)?\n)", body))
        if len(matches) != 1:
            raise ValueError("Unsupported stock: idleMs must be one readonly integer literal")
        match = matches[0]
        structure.at("    interval: idleMs\n", timer)
        start, end = (timer + value for value in match.span(1))
        # Validate the code prefix, not a possibly trailing upstream comment.
        structure.at(body[match.start():match.end()], timer)
        source = source[:start] + str(blank_seconds * 1000) + source[end:]
        compatibility = "idleMs"
    else:
        interval = "    interval: 5000\n"
        _require_one(body, interval, "Service.qml idleBlankTimer")
        position = timer + body.index(interval)
        if (structure.owner(position + 4) != timer
                or structure.code[position:position + len(interval)] != interval):
            raise ValueError("Unsupported stock: idle timer interval scope changed")
        source = source[:position] + f"    interval: {blank_seconds * 1000}\n" + source[position + len(interval):]
        compatibility = "interval"
    source = source.replace("  function runWake() {\n",
        "  property int promptActivitySerial: 0\n\n  function runWake() {\n    promptActivitySerial += 1\n")
    source = source.replace("        passwordText: root.enteredPassword\n",
        "        passwordText: root.enteredPassword\n        promptActivitySerial: root.promptActivitySerial\n")
    source = source.replace(_PREVIEW, _PREVIEW.replace("      loadBackground:", "      previewSaver: true\n      loadBackground:"))
    return source, compatibility


def _patch_view(source: str) -> str:
    structure = _Structure(source)
    structure.at("  function forcePasswordFocus() {\n", structure.root)
    _require_one(source, "  Rectangle {\n    anchors.fill: parent\n    color: Commons.Color.background\n", "LockView.qml background")
    background = source.index("{", source.index("  Rectangle {\n    anchors.fill: parent\n    color: Commons.Color.background\n"))
    if structure.parents.get(background) != structure.root:
        raise ValueError("Unsupported stock: background is not a root child")
    if not source.endswith("  }\n}\n") or structure.ends[background] != len(source) - len("}\n}\n"):
        raise ValueError("Unsupported stock: background must be the last root child")
    field = structure.object_id("inputField", "BorderSurface", background)
    if structure.member_count("opacity", field):
        raise ValueError("Unsupported stock: input field already has opacity")
    password = structure.object_id("passwordInput", "TextInput", field)
    structure.at("        enabled: root.inputEnabled && !root.authenticatingPassword\n", password)
    for index, (marker, replacement) in enumerate(_VIEW_EDITS):
        structure.at(marker, (structure.root if index < 4 else field if index == 4 else password))
    for marker, replacement in _VIEW_EDITS:
        source = source.replace(marker, replacement)
    source = source.replace("  function forcePasswordFocus() {\n",
                            _PROMPT_TIMEOUT + "  function forcePasswordFocus() {\n")
    return source.removesuffix("  }\n}\n") + _SAVER + "  }\n}\n"


def generate_runtime(stock_dir: Path, output_dir: Path, saver_template: Path, *,
                     python_executable: Path, bridge_script: Path,
                     renderer_dir: Path, fps: int = 60,
                     blank_seconds: int = 1800, metrics: bool = False) -> dict:
    """Generate a fresh runtime, publishing it with a same-filesystem rename.

    ``output_dir`` must not exist and must be outside ``stock_dir``. All
    compatibility checks and template substitutions finish before creating it.
    Support-file bytes and modes come from one in-memory input snapshot; only
    Service.qml and LockView.qml receive the small visual/timing edits.

    Returns JSON-serializable metadata: schema_version, source_sha256 (relative
    filename -> original SHA-256), fps, blank_seconds, metrics, compatibility
    (``idleMs`` or ``interval``). Metadata is returned, not written to a file.
    ValueError means invalid options or unsupported/previously patched input;
    FileExistsError protects existing generations; other OSErrors propagate.
    This function never activates a plugin, authenticates, or edits config.
    """
    if type(fps) is not int or not 15 <= fps <= 120:
        raise ValueError("fps must be an integer in 15..120")
    if type(blank_seconds) is not int or not 30 <= blank_seconds <= 86400:
        raise ValueError("blank_seconds must be an integer in 30..86400")
    if type(metrics) is not bool:
        raise ValueError("metrics must be a boolean")
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError("Runtime output must be a new directory")
    if output_dir.resolve().is_relative_to(stock_dir.resolve()):
        raise ValueError("Runtime output must be outside the stock plugin directory")
    files = {}
    modes = {}
    for path in sorted(stock_dir.rglob("*")):
        if path.is_symlink():
            raise ValueError("Stock plugin must not contain symbolic links")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Stock plugin contains a non-regular support file")
        if path.name == "manifest.json":
            continue
        relative = path.relative_to(stock_dir)
        files[relative] = path.read_bytes()
        modes[relative] = stat.S_IMODE(path.stat().st_mode) & 0o777
    if Path("SecureSaver.qml") in files:
        raise ValueError("Stock plugin already contains SecureSaver.qml")
    for name in ("Service.qml", "LockView.qml"):
        if Path(name) not in files:
            raise ValueError(f"Stock plugin is missing {name}")
    rendered = saver_template.read_text(encoding="utf-8")
    replacements = {
        "@@PYTHON@@": json.dumps(str(python_executable)),
        "@@BRIDGE@@": json.dumps(str(bridge_script)),
        "@@RENDERER_URL@@": json.dumps(renderer_dir.resolve().as_uri()),
        "@@FPS@@": str(fps),
        "@@METRICS@@": json.dumps(metrics),
    }
    template_code = _code_mask(rendered)
    for token in replacements:
        _require_one(rendered, token, "SecureSaver template")
        position = rendered.index(token)
        if template_code[position:position + len(token)] != token:
            raise ValueError("SecureSaver template tokens must be unquoted code, not comments")
    unmatched = rendered
    for token in replacements:
        unmatched = unmatched.replace(token, "")
    if "@@" in unmatched:
        raise ValueError("SecureSaver template contains unknown or malformed tokens")
    # Replace only the original template matches, never token-looking path text.
    rendered = re.sub(r"@@[A-Z_]+@@", lambda match: replacements[match[0]], rendered)
    metadata = {"schema_version": 1,
                "source_sha256": {p.as_posix(): hashlib.sha256(data).hexdigest()
                                  for p, data in sorted(files.items())},
                "fps": fps, "blank_seconds": blank_seconds, "metrics": metrics}
    for name in ("Service.qml", "LockView.qml"):
        source = files[Path(name)].decode("utf-8")
        if re.search(r"\b(?:SecureSaver|saverActive|saverDismissed|previewSaver|dismissSaver|promptIdleTimer|promptActivitySerial|onPromptActivitySerialChanged)\b", source):
            raise ValueError(f"Stock {name} is already patched or has conflicting saver identifiers")
    service, compatibility = _patch_service(files[Path("Service.qml")].decode("utf-8"), blank_seconds)
    files[Path("Service.qml")] = service.encode("utf-8")
    files[Path("LockView.qml")] = _patch_view(files[Path("LockView.qml")].decode("utf-8")).encode("utf-8")
    metadata["compatibility"] = compatibility
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        for relative, data in files.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(modes[relative])
        (staging / "SecureSaver.qml").write_bytes(rendered.encode("utf-8"))
        staging.rename(output_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return metadata
