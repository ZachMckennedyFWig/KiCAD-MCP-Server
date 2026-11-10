"""Load a project "standards profile" (MCP_ISSUES §2.9, principle 8: policy as data).

The profile lives next to the project as ``standards.yaml`` / ``standards.yml``
/ ``standards.json`` and describes the fab's limits, plane intent, regions
with allowed nets, net classes with routing intent, and design checks::

    fab: { name: jlcpcb, layers: 4, min_track: 0.1, min_space: 0.1, min_drill: 0.3,
           min_annular: 0.1, silk_min_height: 0.8, silk_min_width: 0.15 }
    planes:
      In1: { net: GND, split: false }
    regions:
      analog: { polygon: [[66,62],[92,62],[100,100],[66,100]],
                allowed_nets: ["/Audio/*", GND, "+3V3"] }
    netclasses:
      FAST:   { nets: ["I2S_*", "USB_*"], no_plane_split_crossing: true, prefer: F.Cu }
      PWR_HI: { width: 1.0, nets: ["/Power/+12V"] }
    nets:
      "+12V": { forbid_regions: [analog] }
    checks:
      every_gnd_smd_pad_has_via_within: 1.3
      exposed_pads_have_via_array: true
      decoupling_within_mm: 3

PyYAML is used when it is installed. KiCad's bundled Python does not ship it,
so a small parser for the subset above (block mappings and lists, flow
``{}``/``[]`` collections that may span lines, quoted and plain scalars,
``#`` comments) is the fallback. JSON needs nothing extra.
"""

import fnmatch
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

PROFILE_NAMES = ("standards.yaml", "standards.yml", "standards.json")


class ProfileError(ValueError):
    """A profile that cannot be read; the message says where and why."""


# ---------------------------------------------------------------------------
# Minimal YAML subset
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")


def _scalar(text: str) -> Any:
    t = text.strip()
    if t == "":
        return None
    if (t[0] == '"' and t[-1] == '"' and len(t) >= 2) or (
        t[0] == "'" and t[-1] == "'" and len(t) >= 2
    ):
        return _unquote(t)
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if _NUMBER.match(t):
        try:
            return int(t) if re.match(r"^[-+]?\d+$", t) else float(t)
        except ValueError:
            pass
    return t


def _unquote(t: str) -> str:
    if t[0] == "'":
        return t[1:-1].replace("''", "'")
    try:
        return json.loads(t)
    except ValueError:
        return t[1:-1]


def _strip_comment(line: str) -> str:
    quote = None
    for i, c in enumerate(line):
        if quote:
            if c == "\\" and quote == '"':
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            # Only a quote that starts a token opens a string.
            if i == 0 or line[i - 1] in " \t[{,:":
                quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


class _Flow:
    """Recursive-descent parser for flow collections: ``{a: 1, b: [x, "y"]}``."""

    def __init__(self, text: str, where: str):
        self.s = text
        self.i = 0
        self.where = where

    def error(self, msg: str) -> ProfileError:
        return ProfileError(f"{self.where}: {msg} near {self.s[self.i:self.i + 30]!r}")

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def value(self) -> Any:
        self.ws()
        if self.i >= len(self.s):
            raise self.error("unexpected end of value")
        c = self.s[self.i]
        if c == "{":
            return self.mapping()
        if c == "[":
            return self.sequence()
        if c in "\"'":
            return self.quoted()
        return _scalar(self.plain())

    def quoted(self) -> str:
        q = self.s[self.i]
        j = self.i + 1
        while j < len(self.s):
            if q == '"' and self.s[j] == "\\":
                j += 2
                continue
            if self.s[j] == q:
                if q == "'" and j + 1 < len(self.s) and self.s[j + 1] == "'":
                    j += 2
                    continue
                break
            j += 1
        if j >= len(self.s):
            raise self.error("unterminated string")
        text = self.s[self.i : j + 1]
        self.i = j + 1
        return _unquote(text)

    def plain(self, stop: str = ",]}") -> str:
        j = self.i
        while j < len(self.s) and self.s[j] not in stop:
            if (
                self.s[j] == ":"
                and ":" in stop
                and (j + 1 >= len(self.s) or self.s[j + 1] in " \t")
            ):
                break
            j += 1
        text = self.s[self.i : j]
        self.i = j
        return text.strip()

    def mapping(self) -> Dict[str, Any]:
        self.i += 1
        out: Dict[str, Any] = {}
        while True:
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == "}":
                self.i += 1
                return out
            if self.s[self.i] in "\"'":
                key = self.quoted()
            else:
                key = self.plain(":,}")
            self.ws()
            if self.i >= len(self.s) or self.s[self.i] != ":":
                raise self.error(f"expected ':' after key {key!r}")
            self.i += 1
            out[str(key)] = self.value()
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == "}":
                continue
            else:
                raise self.error("expected ',' or '}'")

    def sequence(self) -> List[Any]:
        self.i += 1
        out: List[Any] = []
        while True:
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == "]":
                self.i += 1
                return out
            out.append(self.value())
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == "]":
                continue
            else:
                raise self.error("expected ',' or ']'")


def _balance(text: str) -> int:
    depth = 0
    quote = None
    for i, c in enumerate(text):
        if quote:
            if c == "\\" and quote == '"':
                continue
            if c == quote:
                quote = None
        elif c in "\"'" and (i == 0 or text[i - 1] in " \t[{,:"):
            quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
    return depth


def _split_key(text: str) -> Optional[Tuple[str, str]]:
    """``key: rest`` -> (key, rest); None when the line is not a mapping entry."""
    t = text
    if t and t[0] in "\"'":
        q = t[0]
        j = t.find(q, 1)
        while q == '"' and j > 0 and t[j - 1] == "\\":
            j = t.find(q, j + 1)
        if j < 0:
            return None
        key = _unquote(t[: j + 1])
        rest = t[j + 1 :].lstrip()
        if not rest.startswith(":"):
            return None
        return str(key), rest[1:].strip()
    m = re.match(r"^([^:#{}\[\],]+?):(\s+|$)(.*)$", t)
    if not m:
        return None
    return m.group(1).strip(), m.group(3).strip()


def parse_yaml_subset(text: str, where: str = "standards.yaml") -> Any:
    """Parse the YAML subset described in the module docstring."""
    raw = text.replace("\t", "    ").splitlines()
    lines: List[Tuple[int, str, int]] = []  # (indent, content, line number)
    i = 0
    while i < len(raw):
        content = _strip_comment(raw[i])
        lineno = i + 1
        i += 1
        if not content.strip() or content.strip() in ("---", "..."):
            continue
        # Join continuation lines of an open flow collection.
        while _balance(content) > 0 and i < len(raw):
            content += " " + _strip_comment(raw[i]).strip()
            i += 1
        if _balance(content) != 0:
            raise ProfileError(f"{where}:{lineno}: unbalanced brackets")
        indent = len(content) - len(content.lstrip(" "))
        lines.append((indent, content.strip(), lineno))

    pos = 0

    def value_of(rest: str, lineno: int, child_indent: int) -> Any:
        nonlocal pos
        if rest == "" or rest in ("|", ">"):
            if pos < len(lines) and lines[pos][0] >= child_indent:
                return block(lines[pos][0])
            return None
        if rest[0] in "{[":
            parser = _Flow(rest, f"{where}:{lineno}")
            val = parser.value()
            parser.ws()
            if parser.i != len(parser.s):
                raise parser.error("unexpected text after collection")
            return val
        return _scalar(rest)

    def block(indent: int) -> Any:
        nonlocal pos
        if pos >= len(lines):
            return None
        first = lines[pos][1]
        if first == "-" or first.startswith("- "):
            out_list: List[Any] = []
            while (
                pos < len(lines)
                and lines[pos][0] == indent
                and (lines[pos][1] == "-" or lines[pos][1].startswith("- "))
            ):
                _, content, lineno = lines[pos]
                pos += 1
                rest = content[1:].strip()
                kv = _split_key(rest) if rest and rest[0] not in "{[" else None
                if kv is not None:
                    # "- key: value" starts a mapping whose other keys are indented.
                    item: Dict[str, Any] = {}
                    item[kv[0]] = value_of(kv[1], lineno, indent + 2)
                    if pos < len(lines) and lines[pos][0] > indent:
                        more = block(lines[pos][0])
                        if not isinstance(more, dict):
                            raise ProfileError(f"{where}:{lineno}: mixed list item")
                        item.update(more)
                    out_list.append(item)
                else:
                    out_list.append(value_of(rest, lineno, indent + 1))
            return out_list
        out: Dict[str, Any] = {}
        while pos < len(lines) and lines[pos][0] == indent:
            _, content, lineno = lines[pos]
            kv = _split_key(content)
            if kv is None:
                raise ProfileError(f"{where}:{lineno}: expected 'key: value', got {content!r}")
            pos += 1
            key, rest = kv
            if (
                rest == ""
                and pos < len(lines)
                and lines[pos][0] == indent
                and (lines[pos][1].startswith("- ") or lines[pos][1] == "-")
            ):
                out[key] = block(indent)  # "key:" followed by a list at the same indent
            else:
                out[key] = value_of(rest, lineno, indent + 1)
        if pos < len(lines) and lines[pos][0] > indent:
            raise ProfileError(f"{where}:{lines[pos][2]}: unexpected indentation")
        return out

    if not lines:
        return {}
    result = block(lines[0][0])
    if pos < len(lines):
        raise ProfileError(f"{where}:{lines[pos][2]}: unexpected indentation")
    return result


def parse_profile_text(text: str, name: str = "standards.yaml") -> Dict[str, Any]:
    if name.lower().endswith(".json"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ProfileError(f"{name}: invalid JSON: {exc}")
    else:
        try:
            import yaml  # type: ignore

            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise ProfileError(f"{name}: invalid YAML: {exc}")
        except ImportError:
            data = parse_yaml_subset(text, name)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ProfileError(f"{name}: the top level must be a mapping")
    return data


# ---------------------------------------------------------------------------
# Locating and normalising
# ---------------------------------------------------------------------------


def find_profile(project_dir: Optional[str]) -> Optional[str]:
    if not project_dir or not os.path.isdir(project_dir):
        return None
    for name in PROFILE_NAMES:
        path = os.path.join(project_dir, name)
        if os.path.isfile(path):
            return path
    return None


def load_profile(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return normalize_profile(parse_profile_text(text, os.path.basename(path)))


def layer_name(name: Any) -> str:
    """``In1`` -> ``In1.Cu``, ``F`` / ``top`` -> ``F.Cu``; others unchanged."""
    s = str(name).strip()
    low = s.lower()
    if low in ("f", "top", "front"):
        return "F.Cu"
    if low in ("b", "bottom", "back"):
        return "B.Cu"
    if re.match(r"^in\d+$", low):
        return "In" + s[2:] + ".Cu"
    return s


def _list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def normalize_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    """Fill defaults and canonical names; raise ProfileError on wrong shapes."""
    from utils.poly2d import parse_polygon

    out: Dict[str, Any] = {
        "fab": dict(data.get("fab") or {}),
        "planes": {},
        "regions": {},
        "netclasses": {},
        "nets": {},
        "checks": dict(data.get("checks") or {}),
    }
    for section in ("fab", "planes", "regions", "netclasses", "nets", "checks"):
        val = data.get(section)
        if val is not None and not isinstance(val, dict):
            raise ProfileError(f"'{section}' must be a mapping")

    for layer, spec in (data.get("planes") or {}).items():
        spec = spec if isinstance(spec, dict) else {"net": spec}
        out["planes"][layer_name(layer)] = {
            "net": spec.get("net"),
            "split": bool(spec.get("split", bool(spec.get("regions")))),
            "regions": dict(spec.get("regions") or {}),
        }

    for name, spec in (data.get("regions") or {}).items():
        if not isinstance(spec, dict) or "polygon" not in spec:
            raise ProfileError(f"region '{name}' needs a polygon")
        try:
            poly = parse_polygon(spec["polygon"])
        except ValueError as exc:
            raise ProfileError(f"region '{name}': {exc}")
        out["regions"][str(name)] = {
            "polygon": poly,
            "allowed_nets": [str(n) for n in _list(spec.get("allowed_nets"))] or None,
            "forbidden_nets": [str(n) for n in _list(spec.get("forbidden_nets"))],
            "layers": [layer_name(x) for x in _list(spec.get("layers"))] or None,
        }

    for name, spec in (data.get("netclasses") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        out["netclasses"][str(name)] = {
            "nets": [str(n) for n in _list(spec.get("nets"))],
            "no_plane_split_crossing": bool(spec.get("no_plane_split_crossing", False)),
            "prefer": layer_name(spec["prefer"]) if spec.get("prefer") else None,
            "width": float(spec["width"]) if spec.get("width") is not None else None,
            "forbid_regions": [str(r) for r in _list(spec.get("forbid_regions"))],
        }

    for name, spec in (data.get("nets") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        out["nets"][str(name)] = {
            "forbid_regions": [str(r) for r in _list(spec.get("forbid_regions"))],
        }

    for region_ref in [r for nc in out["netclasses"].values() for r in nc["forbid_regions"]] + [
        r for n in out["nets"].values() for r in n["forbid_regions"]
    ]:
        if region_ref not in out["regions"]:
            raise ProfileError(f"forbid_regions names unknown region '{region_ref}'")
    return out


def net_matches(net: str, pattern: str) -> bool:
    """Glob match; a pattern without '/' also matches the hierarchical leaf.

    ``+5V`` matches ``/Power/+5V``; ``/Power/*`` matches only that sheet.
    """
    if not net:
        return False
    if fnmatch.fnmatchcase(net, pattern):
        return True
    if "/" not in pattern and "/" in net:
        return fnmatch.fnmatchcase(net.rsplit("/", 1)[-1], pattern)
    return False


def net_matches_any(net: str, patterns: List[str]) -> bool:
    return any(net_matches(net, p) for p in patterns)
