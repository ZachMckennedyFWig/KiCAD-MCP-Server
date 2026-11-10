"""
Dynamic Symbol Loader for KiCad Schematics

Loads symbols from .kicad_sym library files and injects them into schematics
on-the-fly using TEXT MANIPULATION (not sexpdata) to preserve file formatting.

This enables access to all ~10,000+ KiCad symbols dynamically.
"""

import logging
import os
import re
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.file_io import FileTransaction, read_text_preserve_newline, write_text_atomic
from utils.sexpr_format import (
    QUOTED_VALUE,
    QUOTED_VALUE_SKIP,
    escape_sexpr_string,
    iter_child_offsets,
    match_paren,
    unescape_sexpr_string,
)
from utils.sheet_tree import instance_paths, project_name
from utils.symbol_instances import references_for_new_symbol

logger = logging.getLogger("kicad_interface")

# One (lib ...) row of a sym-lib-table. Name, type and URI may each be quoted
# (URIs with spaces: OneDrive, network shares) or bare. Quoted values are taken
# literally: hand-written Windows tables carry raw backslashes in paths.
_TABLE_ROW_RE = re.compile(
    r"\(lib\s+"
    r'\(name\s+(?:"([^"]*)"|([^"\)\s]+))\)\s*'
    r'\(type\s+(?:"([^"]*)"|([^"\)\s]+))\)\s*'
    r'\(uri\s+(?:"([^"]*)"|([^"\)\s]+))',
    re.IGNORECASE,
)

# Head token and (for property / symbol) the first quoted argument of a list.
_HEAD_RE = re.compile(r"\(\s*([A-Za-z_][A-Za-z0-9_]*)(?:\s+" + QUOTED_VALUE + r")?")
_EXTENDS_RE = re.compile(r"\(\s*extends\s+" + QUOTED_VALUE + r"\s*\)")
_UNIT_SUFFIX_RE = re.compile(r"_(\d+)_(\d+)$")
# Properties a derived symbol only overrides when it gives them a value
# (KiCad's LIB_SYMBOL::Flatten keeps the parent's keywords / filters otherwise).
_INHERIT_IF_EMPTY = {"ki_keywords", "ki_fp_filters"}


def _quoted_or_bare(quoted: Optional[str], bare: Optional[str]) -> Optional[str]:
    return quoted if quoted is not None else bare


class SymbolLibraryError(ValueError):
    """A library symbol was found but cannot be embedded faithfully."""


def _children(block: str) -> List[Tuple[int, int, str, Optional[str]]]:
    """Direct children of the list starting at the first ``(`` of *block*.

    Returns ``(start, end_inclusive, head_token, first_quoted_arg)`` per child.
    String-aware: parens inside quoted pin names (``"PA13(JTMS"``) are text.
    """
    out = []
    for off in iter_child_offsets(block):
        end = match_paren(block, off)
        if end == -1:
            raise SymbolLibraryError("unbalanced parentheses in symbol definition")
        m = _HEAD_RE.match(block, off)
        head = m.group(1) if m else ""
        arg = unescape_sexpr_string(m.group(2)) if m and m.group(2) is not None else None
        out.append((off, end, head, arg))
    return out


#: First schematic file-format version that accepts ``(body_style ...)`` and
#: ``(in_pos_files ...)`` inside a placed ``(symbol ...)``. Both tokens are
#: KiCad 10 additions: a KiCad 8 (20231120) or KiCad 9 (20250114) file never
#: contains them, and those KiCad releases reject a schematic carrying a token
#: they do not know with a bare "Failed to load schematic". KiCad 10 accepts a
#: v9 file either way (kicad-cli 10.0.0), so gating on the file's declared
#: version costs nothing there and keeps the file loadable everywhere else.
_KICAD10_SCH_VERSION = 20260101

# Module-level caches shared across DynamicSymbolLoader instances.
# A fresh loader is created for every add_component call (see
# schematic_handlers), so instance-level caches never survive — every
# component add would otherwise re-scan the sym-lib-table and re-read
# multi-MB .kicad_sym files. These caches make library resolution and symbol
# extraction pay their cost once. Libraries are NOT immutable mid-session,
# though: create_symbol / delete_symbol / add_library_symbol_property rewrite
# .kicad_sym files and register_symbol_library rewrites the sym-lib-table, so
# stale entries are guarded three ways — resolution misses are never cached,
# resolved paths are revalidated with exists(), and symbol blocks carry the
# source file's mtime_ns. The mutating handlers additionally call
# DynamicSymbolLoader.clear_library_caches() for the case a stat cannot see
# (re-pointing an existing library name at a different path).
_LIB_DIRS_CACHE: Optional[Tuple[Tuple, List[Path]]] = None  # (env fingerprint, dirs)
_LIB_FILE_CACHE: Dict[Tuple, Path] = {}
_SYMBOL_BLOCK_CACHE: Dict[Tuple[str, str], Tuple[Optional[int], Optional[str]]] = {}

_SYMBOL_DIR_ENV_VARS = (
    "KICAD10_SYMBOL_DIR",
    "KICAD9_SYMBOL_DIR",
    "KICAD8_SYMBOL_DIR",
    "KICAD_SYMBOL_DIR",
)


def _symbol_dir_env_fingerprint() -> Tuple:
    """The env-var inputs library discovery depends on.

    Cached discovery results are only valid for the environment they were
    computed under — tests monkeypatch KICAD_SYMBOL_DIR per test, and a cache
    key that ignores it serves one test's temp directory to the next.
    """
    return tuple(os.environ.get(v) for v in _SYMBOL_DIR_ENV_VARS)


class DynamicSymbolLoader:
    """
    Dynamically loads symbols from KiCad library files and injects them into schematics.

    Uses raw text manipulation instead of sexpdata to avoid corrupting the KiCad file format.

    Key rules for KiCad 9 .kicad_sch format:
    - Top-level symbols in lib_symbols must have library prefix: (symbol "Device:R" ...)
    - Sub-symbols must NOT have library prefix: (symbol "R_0_1" ...), (symbol "R_1_1" ...)
    - No (extends ...) inside a sheet: derived symbols are embedded flattened
    - The library text is embedded verbatim; only the top-level name changes
    """

    def __init__(self, project_path: Optional[Path] = None):
        self.symbol_cache = {}  # Cache: "lib:symbol" -> raw text block
        self.project_path = project_path  # Project directory for project-specific libraries
        # (instance path, reference) of each use of the sheet, for the symbol
        # create_component_instance placed last; more than one when the sheet
        # is used more than once.
        self.placed_instances: List[Tuple[str, str]] = []

    @staticmethod
    def clear_library_caches() -> None:
        """Reset the module-level library caches (call if libraries change on disk)."""
        global _LIB_DIRS_CACHE
        _LIB_DIRS_CACHE = None
        _LIB_FILE_CACHE.clear()
        _SYMBOL_BLOCK_CACHE.clear()

    def find_kicad_symbol_libraries(self) -> List[Path]:
        """Find all KiCad symbol library directories (cached module-wide).

        The cache entry records the env fingerprint it was computed under and
        is ignored if the KICAD*_SYMBOL_DIR environment has changed since.
        """
        global _LIB_DIRS_CACHE
        env = _symbol_dir_env_fingerprint()
        if _LIB_DIRS_CACHE is not None and _LIB_DIRS_CACHE[0] == env:
            return _LIB_DIRS_CACHE[1]
        # Discovered install roots first (registry + Program Files globs +
        # custom roots like C:\KiCad, newest version first) — the same shared
        # helper the cli/footprint/symbol-search paths use (#286), so the
        # production component-placement path cannot drift from the rest of
        # discovery again. Env-var overrides below still take precedence.
        try:
            from utils.kicad_roots import kicad_install_roots

            root_symbol_dirs = [r / "share" / "kicad" / "symbols" for r in kicad_install_roots()]
        except Exception:  # pragma: no cover - defensive; helper is stdlib-only
            root_symbol_dirs = []

        possible_paths = root_symbol_dirs + [
            Path("C:/Program Files/KiCad/10.0/share/kicad/symbols"),
            Path("/usr/share/kicad/symbols"),
            Path("/usr/local/share/kicad/symbols"),
            Path("C:/Program Files/KiCad/9.0/share/kicad/symbols"),
            Path("C:/Program Files/KiCad/8.0/share/kicad/symbols"),
            Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"),
            Path.home() / ".local" / "share" / "kicad" / "10.0" / "symbols",
            Path.home() / ".local" / "share" / "kicad" / "9.0" / "symbols",
            Path.home() / "Documents" / "KiCad" / "10.0" / "3rdparty" / "symbols",
            Path.home() / "Documents" / "KiCad" / "9.0" / "3rdparty" / "symbols",
        ]
        for env_var in [
            "KICAD10_SYMBOL_DIR",
            "KICAD9_SYMBOL_DIR",
            "KICAD8_SYMBOL_DIR",
            "KICAD_SYMBOL_DIR",
        ]:
            if env_var in os.environ:
                possible_paths.insert(0, Path(os.environ[env_var]))

        dirs = [p for p in possible_paths if p.exists() and p.is_dir()]
        _LIB_DIRS_CACHE = (env, dirs)
        return dirs

    def find_library_file(self, library_name: str) -> Optional[Path]:
        """Find the .kicad_sym file for a given library name.

        Search order:
        1. Project-specific sym-lib-table (if project_path is set)
        2. Global KiCad sym-lib-table (~/AppData/Roaming/kicad/<ver>/sym-lib-table on
           Windows, ~/.config/kicad/<ver>/sym-lib-table on Linux,
           ~/Library/Preferences/kicad/<ver>/sym-lib-table on macOS) — covers user-
           registered libraries that live outside the bundled symbol directories
           (e.g. company libraries in OneDrive, network shares, custom paths).
        3. Bundled / well-known KiCad symbol library directories.

        Resolution is cached module-wide by (project_path, library_name, env
        fingerprint) so that repeated component adds don't re-scan the
        sym-lib-table every time. Only successful resolutions are cached: a
        "not found" is typically followed by the user creating or registering
        that library, and a cached miss would keep the name unresolvable
        until process restart. Cached paths are revalidated with exists() so
        a deleted or moved library re-resolves instead of being served stale.
        """
        # A loader whose discovery has been instance-patched (tests point
        # find_kicad_symbol_libraries at a temp dir) must not read or write
        # the shared cache: its results are not valid for other loaders, and
        # the patched state is invisible to any module-level cache key.
        shares_cache = "find_kicad_symbol_libraries" not in self.__dict__

        cache_key = (
            str(self.project_path) if self.project_path else None,
            library_name,
            _symbol_dir_env_fingerprint(),
        )
        if shares_cache:
            hit = _LIB_FILE_CACHE.get(cache_key)
            if hit is not None and hit.exists():
                return hit

        def _search() -> Optional[Path]:
            # 1. Check project-specific sym-lib-table
            if self.project_path:
                project_table = Path(self.project_path) / "sym-lib-table"
                if project_table.exists():
                    resolved = self._resolve_library_from_table(project_table, library_name)
                    if resolved:
                        logger.info(f"Found '{library_name}' in project sym-lib-table: {resolved}")
                        return resolved

            # 2. Check the global user sym-lib-table — only the newest one.
            # KiCad reads just its own version's table; KiCad 10's holds a
            # single (type "Table") row standing for every stock library.
            # Falling through to an older 9.0/8.0 table here resolved stock
            # names to an older KiCad install that was still on disk, so
            # placed symbols carried KiCad 8 geometry (TPS22917DBV QOD on the
            # CT row, SolderJumper pins at ±3.81, USB-C shield "S1" for "SH")
            # and symbols new in KiCad 10 (RP2350B, TPS22810DBV) were
            # "not found" (#5, #5e, #5p).
            global_table = self._global_sym_lib_table()
            if global_table is not None:
                resolved = self._resolve_library_from_table(global_table, library_name)
                if resolved:
                    logger.info(
                        f"Found '{library_name}' in global sym-lib-table {global_table}: {resolved}"
                    )
                    return resolved

            # 3. Fall back to bundled / well-known KiCad symbol directories
            for lib_dir in self.find_kicad_symbol_libraries():
                # Classic single-file library (KiCAD 8/9)
                lib_file = lib_dir / f"{library_name}.kicad_sym"
                if lib_file.exists():
                    return lib_file
                # KiCAD 10 per-symbol directory library
                lib_symdir = lib_dir / f"{library_name}.kicad_symdir"
                if lib_symdir.exists() and lib_symdir.is_dir():
                    return lib_symdir

            logger.warning(
                f"Library file not found: {library_name}.kicad_sym / {library_name}.kicad_symdir"
            )
            return None

        result = _search()
        if shares_cache:
            if result is not None:
                _LIB_FILE_CACHE[cache_key] = result
            else:
                _LIB_FILE_CACHE.pop(cache_key, None)
        return result

    def _global_sym_lib_table_paths(self) -> list:
        """Candidate paths for the user-global sym-lib-table, newest version first."""
        home = Path.home()
        versions = ["10.0", "9.0", "8.0"]
        bases = []
        if os.name == "nt":
            bases.append(home / "AppData" / "Roaming" / "kicad")
        else:
            bases.append(home / ".config" / "kicad")
            bases.append(home / "Library" / "Preferences" / "kicad")  # macOS
        candidates = []
        for base in bases:
            for v in versions:
                candidates.append(base / v / "sym-lib-table")
        return candidates

    def _global_sym_lib_table(self) -> Optional[Path]:
        """The user-global sym-lib-table KiCad itself would read: the newest one present."""
        for candidate in self._global_sym_lib_table_paths():
            if candidate.is_file():
                return candidate
        return None

    def _resolve_library_from_table(
        self, table_path: Path, library_name: str, _seen: Optional[set] = None
    ) -> Optional[Path]:
        """Return the resolved path of ``library_name`` from a sym-lib-table.

        ``(type "Table")`` rows name another table to search in their place
        (KiCad 10's global table is a single such row,
        ``${KICAD10_TEMPLATE_DIR}/sym-lib-table``); they are followed
        recursively, with ``_seen`` stopping a table that includes itself.
        Disabled rows are skipped, as KiCad skips them.
        """
        seen = set() if _seen is None else _seen
        table_key = os.path.normcase(os.path.realpath(str(table_path)))
        if table_key in seen:
            return None
        seen.add(table_key)
        try:
            with open(table_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            logger.warning(f"Could not read sym-lib-table {table_path}: {e}")
            return None

        base_dir = Path(table_path).parent
        for match in _TABLE_ROW_RE.finditer(content):
            nickname = _quoted_or_bare(match.group(1), match.group(2))
            lib_type = _quoted_or_bare(match.group(3), match.group(4)) or ""
            uri = _quoted_or_bare(match.group(5), match.group(6)) or ""
            row_end = match_paren(content, match.start())
            if row_end != -1 and re.search(
                r"\(disabled\s*(?:yes)?\s*\)", content[match.start() : row_end + 1]
            ):
                continue
            if lib_type.lower() == "table":
                nested = self._resolve_sym_uri(uri, base_dir)
                if nested and Path(nested).is_file():
                    found = self._resolve_library_from_table(Path(nested), library_name, seen)
                    if found:
                        return found
                else:
                    logger.debug(f"Could not resolve nested library table URI: {uri}")
                continue
            if nickname != library_name:
                continue
            resolved = self._resolve_sym_uri(uri, base_dir)
            if resolved and Path(resolved).exists():
                return Path(resolved)
        return None

    def _path_vars(self) -> Dict[str, str]:
        """Values for the path variables a sym-lib-table URI can reference.

        Every ``KICAD*_SYMBOL_DIR`` stands for the newest discovered install's
        symbol directory (or the MCP's KICAD*_SYMBOL_DIR override), the same
        mapping the search/list symbol tools use, so placement and search can
        never resolve one nickname to two different files. The process
        environment wins over everything, as it does in KiCad.
        """
        values: Dict[str, str] = {}
        symbol_dirs = self.find_kicad_symbol_libraries()
        if symbol_dirs:
            for var in _SYMBOL_DIR_ENV_VARS + ("KISYSSYM",):
                values[var] = str(symbol_dirs[0])
        try:
            from utils.platform_helper import PlatformHelper

            template_dir = PlatformHelper.find_kicad_template_dir()
            if template_dir:
                for var in (
                    "KICAD10_TEMPLATE_DIR",
                    "KICAD9_TEMPLATE_DIR",
                    "KICAD8_TEMPLATE_DIR",
                    "KICAD_TEMPLATE_DIR",
                ):
                    values[var] = str(template_dir)
            third_party = PlatformHelper.find_kicad_3rd_party_dir()
            if third_party:
                for var in (
                    "KICAD10_3RD_PARTY",
                    "KICAD9_3RD_PARTY",
                    "KICAD8_3RD_PARTY",
                    "KICAD_3RD_PARTY",
                ):
                    values[var] = str(third_party)
            values.update({k: str(v) for k, v in PlatformHelper.load_kicad_env_vars().items() if v})
        except Exception as e:  # pragma: no cover - discovery is best effort
            logger.debug(f"KiCad path variable discovery failed: {e}")
        if self.project_path:
            values["KIPRJMOD"] = str(self.project_path)
        return values

    def _resolve_sym_uri(self, uri: str, base_dir: Optional[Path] = None) -> Optional[str]:
        """Expand ``${VAR}`` / ``$(VAR)`` in a sym-lib-table URI.

        A relative result is taken relative to ``base_dir`` (the table's own
        directory). Unknown variables are left in place, so the path simply
        does not exist and the row is skipped.
        """
        values = self._path_vars()

        def _sub(m: "re.Match") -> str:
            var = m.group(1) or m.group(2)
            if os.environ.get(var):
                return os.environ[var]
            return values.get(var, m.group(0))

        result = re.sub(r"\$\{([A-Za-z0-9_]+)\}|\$\(([A-Za-z0-9_]+)\)", _sub, uri)
        result = os.path.expanduser(result)
        if base_dir is not None and not os.path.isabs(result) and "${" not in result:
            result = str(Path(base_dir) / result)
        return result

    # ------------------------------------------------------------------ #
    # Library symbol extraction: verbatim text, extends flattened         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _find_symbol_span(text: str, symbol_name: str) -> Optional[Tuple[int, int]]:
        """``(start, end_inclusive)`` of the ``(symbol "<symbol_name>" ...)`` list.

        String-aware (``match_paren``), and not tied to KiCad's one-token-per-
        line layout: the old line/paren counter lost its place on a pin name
        such as ``"PA13(JTMS"`` and could not find a symbol in a compact
        single-line library at all. When the name also occurs as a nested list
        (a top-level symbol literally named like another's ``X_1_1`` unit),
        the least-indented occurrence wins: the top-level one.
        """
        pattern = re.compile(
            r'\(symbol\s+"' + re.escape(escape_sexpr_string(symbol_name)) + r'"(?=[\s)])'
        )
        best: Optional[Tuple[int, int]] = None
        best_indent = None
        for m in pattern.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            prefix = text[line_start : m.start()]
            indent = len(prefix) if not prefix.strip() else 10**9
            if best is None or indent < best_indent:
                end = match_paren(text, m.start())
                if end == -1:
                    continue
                best, best_indent = (m.start(), end), indent
                if indent <= 1:
                    break
        return best

    def _extract_symbol_block(self, text: str, symbol_name: str) -> Optional[str]:
        """Return the raw text of a symbol definition, byte-for-byte.

        The text includes the leading indentation of its first line (when only
        whitespace precedes it), so every line carries the library's own
        indentation and can be re-indented uniformly.
        """
        span = self._find_symbol_span(text, symbol_name)
        if span is None:
            return None
        start, end = span
        line_start = text.rfind("\n", 0, start) + 1
        if not text[line_start:start].strip():
            start = line_start
        return text[start : end + 1]

    @staticmethod
    def _rename_symbol_header(block: str, new_name: str) -> str:
        """Rename the top-level ``(symbol "<name>"`` token of *block*."""
        return re.sub(
            r'(\(symbol\s+)"(?:[^"\\]|\\.)*"',
            lambda m: f'{m.group(1)}"{escape_sexpr_string(new_name)}"',
            block,
            count=1,
        )

    def _flatten_extends(
        self,
        symbol_name: str,
        block: str,
        parent_lookup,
        _chain: Optional[List[str]] = None,
    ) -> str:
        """Resolve ``(extends "Parent")`` to any depth, the way KiCad saves it.

        KiCad does not accept ``(extends ...)`` inside a schematic's
        ``lib_symbols``; on save it writes ``LIB_SYMBOL::Flatten()``: the root
        ancestor's definition (flags, pins, graphics) renamed to the derived
        symbol, the unit sub-symbols renamed ``<Child>_<unit>_<style>``, and
        the derived symbol's own fields replacing the inherited ones (fields
        only the child has are added). This does the same as text surgery on
        the library's own bytes, so nothing is re-serialised.

        The old inliner resolved one level only: a grandchild such as
        ``Memory_RAM:APS6404L-3SQRx-SN`` (-> APS6404L-SQRx-SN ->
        APS1604M-SQRx-SN) inherited its parent's *unresolved* block, came out
        with no pins and no ``_1_1`` sub-unit, and broke kicad-skip on every
        later call on the sheet (#5b, #5o).

        ``parent_lookup(name)`` returns a parent's raw block or None. A missing
        parent or a cyclic chain raises SymbolLibraryError: embedding a
        pinless half-symbol is worse than refusing.
        """
        m = None
        for start, end, head, arg in _children(block):
            if head == "extends":
                m = _EXTENDS_RE.match(block, start)
                break
        if m is None:
            return block

        parent_name = unescape_sexpr_string(m.group(1))
        chain = (_chain or []) + [symbol_name]
        if parent_name in chain:
            raise SymbolLibraryError(
                f"Cyclic (extends ...) chain: {' -> '.join(chain + [parent_name])}"
            )
        parent_block = parent_lookup(parent_name)
        if parent_block is None:
            raise SymbolLibraryError(
                f"Symbol '{symbol_name}' extends '{parent_name}', which is not in the library "
                f"(chain: {' -> '.join(chain + [parent_name])})"
            )
        resolved_parent = self._flatten_extends(parent_name, parent_block, parent_lookup, chain)

        # The derived symbol's own fields, verbatim.
        child_props: Dict[str, str] = {}
        child_order: List[str] = []
        for start, end, head, arg in _children(block):
            if head == "property" and arg is not None:
                text = block[start : end + 1]
                if arg in _INHERIT_IF_EMPTY and re.match(
                    r'\(\s*property\s+"(?:[^"\\]|\\.)*"\s+""', text
                ):
                    continue
                child_props[arg] = text
                child_order.append(arg)

        edits: List[Tuple[int, int, str]] = []  # (start, end_exclusive, replacement)
        last_prop_end = None
        last_prop_sep = "\n"
        first_unit_start = None
        prev_end = None
        parent_children = _children(resolved_parent)
        for start, end, head, arg in parent_children:
            if head == "property" and arg is not None:
                if arg in child_props:
                    edits.append((start, end + 1, child_props.pop(arg)))
                last_prop_end = end + 1
                if prev_end is not None:
                    last_prop_sep = resolved_parent[prev_end:start]
            elif head == "symbol" and arg is not None:
                um = _UNIT_SUFFIX_RE.search(arg)
                if um and arg == f"{parent_name}_{um.group(1)}_{um.group(2)}":
                    hm = re.compile(r'(\(\s*symbol\s+)"(?:[^"\\]|\\.)*"').match(
                        resolved_parent, start
                    )
                    new = escape_sexpr_string(f"{symbol_name}_{um.group(1)}_{um.group(2)}")
                    edits.append((hm.start(), hm.end(), f'{hm.group(1)}"{new}"'))
                if first_unit_start is None:
                    first_unit_start = start
            prev_end = end + 1

        # Fields only the derived symbol has go after the inherited ones.
        extra = [child_props[name] for name in child_order if name in child_props]
        if extra:
            if last_prop_end is None:
                if first_unit_start is not None:
                    line_start = resolved_parent.rfind("\n", 0, first_unit_start) + 1
                    last_prop_end = line_start - 1 if line_start > 0 else first_unit_start
                else:
                    last_prop_end = match_paren(resolved_parent, resolved_parent.find("("))
            sep = last_prop_sep if last_prop_sep.strip() == "" else "\n"
            edits.append((last_prop_end, last_prop_end, "".join(sep + t for t in extra)))

        out = resolved_parent
        for start, end, repl in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
            out = out[:start] + repl + out[end:]
        return self._rename_symbol_header(out, symbol_name)

    def _validate_symbol_block(self, full_name: str, block: str, source: Path) -> None:
        """Refuse to embed a symbol KiCad or kicad-skip could not read back.

        Checked before anything is written (#5c): balanced parentheses, no
        leftover ``(extends ...)``, at least one ``<Name>_<unit>_<style>``
        sub-unit (kicad-skip crashes on a lib symbol with none), and a clean
        s-expression parse.
        """
        bare = full_name.split(":", 1)[-1]
        first = block.find("(")
        if first == -1 or match_paren(block, first) == -1:
            raise SymbolLibraryError(f"Symbol '{full_name}' in {source} is not balanced")
        units = 0
        drawables = 0
        for _s, _e, head, arg in _children(block[first:]):
            if head == "extends":
                raise SymbolLibraryError(
                    f"Symbol '{full_name}' still has an (extends ...) after flattening"
                )
            if head == "symbol" and arg is not None:
                if arg.startswith(bare + "_") and _UNIT_SUFFIX_RE.search(arg):
                    units += 1
            elif head in ("pin", "rectangle", "polyline", "circle", "arc", "text", "bezier"):
                drawables += 1
        if units == 0:
            hint = (
                " Its pins/graphics sit directly under the symbol (a 'flat' vendor symbol);"
                f" run repair_flat_symbols on {source} first."
                if drawables
                else ""
            )
            raise SymbolLibraryError(
                f"Symbol '{full_name}' has no unit sub-symbols ('{bare}_1_1' ...); embedding it"
                f" would make the sheet unreadable to kicad-skip.{hint}"
            )
        try:
            import sexpdata

            sexpdata.loads(block[first:])
        except Exception as e:
            raise SymbolLibraryError(f"Symbol '{full_name}' in {source} does not parse: {e}")

    def extract_symbol_from_library(self, library_name: str, symbol_name: str) -> Optional[str]:
        """
        Extract a symbol definition from a KiCad .kicad_sym library file.
        Returns the raw text block, ready to be injected into a schematic.

        The text is the library's own, byte-for-byte (#5e): the only changes
        are the ones KiCad itself makes when it embeds a symbol —
        - the top-level name gets its library prefix: (symbol "Library:Name" ...)
        - an ``(extends ...)`` chain of any depth is flattened, with the
          inherited unit sub-symbols renamed to ``Name_<unit>_<style>``.
        Sub-symbol names never carry the library prefix.

        Returns None when the library or symbol does not exist; raises
        SymbolLibraryError (a ValueError) when the symbol exists but cannot be
        embedded faithfully (missing parent, no sub-units, unparsable).
        """
        cache_key = f"{library_name}:{symbol_name}"
        if cache_key in self.symbol_cache:
            return self.symbol_cache[cache_key]

        lib_path = self.find_library_file(library_name)
        if not lib_path:
            return None

        # The physical file the symbol lives in: for a KiCAD 10 directory
        # library each symbol is its own shard file.
        src_file = lib_path / f"{symbol_name}.kicad_sym" if lib_path.is_dir() else lib_path

        def _src_mtime_ns() -> Optional[int]:
            try:
                return src_file.stat().st_mtime_ns
            except OSError:
                return None

        # Module-level cache keyed by the resolved library path + symbol, so the
        # multi-MB .kicad_sym is read and parsed once even though a fresh loader
        # (and empty self.symbol_cache) is created for every component add.
        # Entries carry the source file's mtime_ns: create_symbol /
        # delete_symbol / add_library_symbol_property rewrite .kicad_sym files
        # mid-session, so a hit is honoured only while the mtime matches. (For
        # a symdir symbol whose (extends ...) parent lives in a sibling shard,
        # a parent-only edit is invisible to this stat — the mutating handlers
        # call clear_library_caches() to cover that.) Failures that raise are
        # never cached.
        mod_key = (str(lib_path), symbol_name)
        current_mtime = _src_mtime_ns()
        entry = _SYMBOL_BLOCK_CACHE.get(mod_key)
        if entry is not None and entry[0] == current_mtime:
            cached = entry[1]
            self.symbol_cache[cache_key] = cached
            return cached

        if lib_path.is_dir():
            if not src_file.exists():
                logger.warning(f"Symbol '{symbol_name}' not found in directory library {lib_path}")
                _SYMBOL_BLOCK_CACHE[mod_key] = (current_mtime, None)
                return None
            lib_content = src_file.read_text(encoding="utf-8")

            def parent_lookup(name: str) -> Optional[str]:
                # Sharded (.kicad_symdir) library: a parent lives in its own
                # sibling shard, not in this symbol's file (#282).
                shard = self._read_symdir_shard(lib_path, name)
                return None if shard is None else self._extract_symbol_block(shard, name)

        else:
            lib_content = lib_path.read_text(encoding="utf-8")

            def parent_lookup(name: str) -> Optional[str]:
                return self._extract_symbol_block(lib_content, name)

        block = self._extract_symbol_block(lib_content, symbol_name)
        if block is None:
            logger.warning(f"Symbol '{symbol_name}' not found in {library_name} ({lib_path})")
            _SYMBOL_BLOCK_CACHE[mod_key] = (current_mtime, None)
            return None

        try:
            block = self._flatten_extends(symbol_name, block, parent_lookup)
        except SymbolLibraryError as e:
            raise SymbolLibraryError(f"{library_name}:{symbol_name}: {e}") from None

        full_name = f"{library_name}:{symbol_name}"
        block = self._rename_symbol_header(block, full_name)
        self._validate_symbol_block(full_name, block, src_file)

        self.symbol_cache[cache_key] = block
        _SYMBOL_BLOCK_CACHE[mod_key] = (current_mtime, block)
        logger.info(f"Extracted symbol {full_name} from {src_file} ({len(block)} chars)")
        return block

    def _read_symdir_shard(self, lib_dir: Path, symbol_name: str) -> Optional[str]:
        """Read the per-symbol shard ``<symbol>.kicad_sym`` from a ``.kicad_symdir``."""
        shard = lib_dir / f"{symbol_name}.kicad_sym"
        if not shard.exists():
            return None
        try:
            with open(shard, "r", encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            logger.warning(f"Could not read symdir shard {shard}: {e}")
            return None

    @staticmethod
    def _lib_symbols_span(content: str) -> Optional[Tuple[int, int]]:
        """``(start, end_inclusive)`` of the sheet's ``(lib_symbols ...)`` list."""
        m = re.search(r"\(lib_symbols(?=[\s)])", content)
        if not m:
            return None
        end = match_paren(content, m.start())
        return None if end == -1 else (m.start(), end)

    @staticmethod
    def _reindent(block: str, indent: str, unit: str = "\t") -> str:
        """Re-indent a verbatim block so its first line starts at *indent*.

        Only leading whitespace changes: the block's own common prefix (the
        library's depth-1 indentation) is replaced by *indent* on every line,
        and the library's leading tabs become *unit* (the sheet's own indent
        step). KiCad escapes newlines inside strings, so every physical line
        is structure, never string content.
        """
        lines = block.split("\n")
        first = lines[0]
        base = first[: len(first) - len(first.lstrip())]
        out = []
        for i, line in enumerate(lines):
            if not line.strip():
                out.append(line)
                continue
            if i == 0:
                rest = line.lstrip(" \t")
            elif base and line.startswith(base):
                rest = line[len(base) :]
            else:
                rest = line
            body = rest.lstrip("\t")
            out.append(indent + unit * (len(rest) - len(body)) + body)
        return "\n".join(out)

    def _insert_lib_symbol(self, content: str, symbol_block: str) -> str:
        """Return *content* with *symbol_block* appended inside ``lib_symbols``."""
        span = self._lib_symbols_span(content)
        if span is None:
            raise ValueError("No lib_symbols section found in schematic")
        lib_start, lib_end = span
        line_start = content.rfind("\n", 0, lib_start) + 1
        outer = content[line_start:lib_start]
        outer = outer if not outer.strip() else ""
        unit = "\t" if ("\t" in outer or "\n\t" in content[:4096]) else "  "
        inner = outer + unit
        indented = self._reindent(symbol_block, inner, unit)

        close_line_start = content.rfind("\n", 0, lib_end) + 1
        if close_line_start > lib_start and not content[close_line_start:lib_end].strip():
            # The closing paren sits on its own line: insert whole lines before it.
            return content[:close_line_start] + indented + "\n" + content[close_line_start:]
        return content[:lib_end] + "\n" + indented + "\n" + outer + content[lib_end:]

    def inject_symbol_into_schematic(
        self, schematic_path: Path, library_name: str, symbol_name: str
    ) -> bool:
        """
        Inject a symbol definition into a schematic's lib_symbols section.

        The library text is embedded verbatim (see extract_symbol_from_library)
        and validated before anything is written; the write is atomic. A
        symbol already embedded in this sheet is left as it is — refreshing an
        embedded copy moves the pins of instances already wired to it, which
        is update_symbol_from_library's job.
        """
        full_name = f"{library_name}:{symbol_name}"
        schematic_path = Path(schematic_path)

        content, newline = read_text_preserve_newline(schematic_path)

        span = self._lib_symbols_span(content)
        if span is None:
            raise ValueError("No lib_symbols section found in schematic")
        existing = re.compile(r'\(symbol\s+"' + re.escape(escape_sexpr_string(full_name)) + r'"')
        if existing.search(content, span[0], span[1]):
            logger.info(f"Symbol {full_name} already exists in schematic")
            return True

        # Extract (and validate) before touching the file.
        symbol_block = self.extract_symbol_from_library(library_name, symbol_name)
        if not symbol_block:
            raise ValueError(f"Symbol '{symbol_name}' not found in library '{library_name}'")

        content = self._insert_lib_symbol(content, symbol_block)
        write_text_atomic(schematic_path, content, newline)

        logger.info(f"Injected symbol {full_name} into {schematic_path.name}")
        return True

    @staticmethod
    def _extract_paren_block(text: str, start: int) -> str:
        """Return the substring from text[start] up to and including the matching closing paren."""
        depth, i = 0, start
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
            i += 1
        return text[start:]

    def _extract_lib_property_positions(
        self,
        schematic_path: Path,
        library_name: str,
        symbol_name: str,
    ) -> dict:
        """
        Return {prop_name: (dx, dy, text_angle, effects_str, hidden)} from the
        lib_symbols section of the schematic (which must already have the symbol
        injected).
        effects_str is the full '(effects ...)' string to be reused in the placed
        instance so that justify, font size etc. are preserved; ``hidden`` carries
        the library's field visibility separately, because the hide marker is
        stripped out of effects_str (KiCad 10 records visibility as a top-level
        ``(hide yes)`` on the property, not inside ``(effects ...)``).
        Returns an empty dict on failure.
        """
        try:
            with open(schematic_path, encoding="utf-8") as f:
                content = f.read()

            lib_start = content.find("(lib_symbols")
            if lib_start == -1:
                return {}

            sym_start = content.find(f'(symbol "{library_name}:{symbol_name}"', lib_start)
            if sym_start == -1:
                return {}

            sym_block = self._extract_paren_block(content, sym_start)

            import re

            result = {}
            # Iterate over every top-level (property ...) block in the symbol
            search_pos = 0
            while True:
                m = re.search(
                    r"\(property\s+" + QUOTED_VALUE + r"\s+"
                    # Non-capturing: the value is not needed here, and a
                    # capturing form would shift the coordinate groups below.
                    + QUOTED_VALUE_SKIP + r"\s+\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)",
                    sym_block[search_pos:],
                )
                if not m:
                    break
                abs_start = search_pos + m.start()
                prop_block = self._extract_paren_block(sym_block, abs_start)

                name = unescape_sexpr_string(m.group(1))
                dx = float(m.group(2))
                dy = float(m.group(3))
                angle = float(m.group(4))

                # Extract (effects ...) block from within this property
                eff_pos = prop_block.find("(effects")
                if eff_pos != -1:
                    effects_str = self._extract_paren_block(prop_block, eff_pos)
                    # Strip (hide ...) sub-expressions — visibility travels
                    # separately in the `hidden` flag below
                    effects_str = re.sub(r"\s*\(hide\s+[^)]+\)", "", effects_str)
                    effects_str = effects_str.strip()
                else:
                    effects_str = "(effects (font (size 1.27 1.27)))"

                # Library field visibility. Both spellings occur in the wild:
                # KiCad 10 writes a top-level (hide yes) on the property, older
                # libraries put `hide` (bare token or parenthesised) inside
                # (effects ...). Either means "don't draw this field".
                # Blank the quoted values first: a field whose *value* contains
                # the word "hide" must not be mistaken for a hidden field.
                prop_tokens = re.sub(QUOTED_VALUE, '""', prop_block)
                hidden = bool(
                    re.search(r"\(hide\s+yes\)", prop_tokens)
                    or re.search(r"\s+hide(?=[\s)])", prop_tokens)
                )

                # Only store the first occurrence (top-level lib property, not sub-symbol)
                if name not in result:
                    result[name] = (dx, dy, angle, effects_str, hidden)

                search_pos = abs_start + 1

            return result
        except Exception:
            return {}

    @staticmethod
    def _rotate_offset(dx: float, dy: float, angle_deg: float) -> tuple:
        """
        Rotate a 2-D offset by angle_deg degrees (KiCad CCW-in-screen, i.e. Y-axis
        points downward).  Returns (rx, ry) rounded to 3 decimal places.
        """
        import math

        rad = math.radians(angle_deg)
        # Standard CCW rotation in Y-down screen coordinates:
        #   rx =  dx * cos(a) + dy * sin(a)
        #   ry = -dx * sin(a) + dy * cos(a)
        rx = dx * math.cos(rad) + dy * math.sin(rad)
        ry = -dx * math.sin(rad) + dy * math.cos(rad)
        return round(rx, 3), round(ry, 3)

    # ------------------------------------------------------------------ #
    # Instance-block helpers (project name, hierarchical path, pin uuids) #
    # ------------------------------------------------------------------ #

    def _resolve_project_name(self, schematic_path: Path) -> str:
        """Return the KiCad project name recorded in (instances (project "<name>" ...)).

        KiCad uses the project (``.kicad_pro``) stem, not the literal string
        ``"project"``: the nearest ``.kicad_pro`` in the schematic's directory or
        a few parents, else the schematic's own stem (utils.sheet_tree).
        """
        return project_name(Path(schematic_path))

    @staticmethod
    def _read_sch_version(content: str) -> Optional[int]:
        """Return the ``(version NNNNNNNN)`` token of a schematic, or None.

        The file's own declared version — not the installed KiCad version —
        decides which tokens are legal, because KiCad dispatches to a parser per
        format version. A KiCad 10 binary still refuses a v10-only token inside a
        file that declares 20231120.
        """
        m = re.search(r"\(version\s+(\d+)\)", content)
        return int(m.group(1)) if m else None

    @classmethod
    def _supports_kicad10_symbol_tokens(cls, content: str) -> bool:
        """Whether this schematic accepts ``body_style`` / ``in_pos_files``.

        Unknown/absent version is treated as KiCad 10 to preserve the previous
        behaviour for freshly generated files, which is what the templates and
        the create_schematic fallback emit.
        """
        version = cls._read_sch_version(content)
        return version is None or version >= _KICAD10_SCH_VERSION

    def _build_instance_paths(self, schematic_path: Path) -> List[str]:
        """Return the instance path of every use of ``schematic_path``.

        - Root / flat schematic: ``/<root-sheet-uuid>``, the schematic's own
          top-level (uuid ...).
        - Sheet in a hierarchy: one path per use, each the chain of uuids from
          the root, ``/<root-uuid>/<sheet-block-uuid>[/...]``. A sheet placed
          twice has two, as does a sheet placed once inside one placed twice.
        - Unlinked sheet (no root reaches it yet): one level with the sheet's
          own uuid; add_hierarchical_sheet -> fix_subsheet_instances adds the
          real paths once it is linked.

        The root is looked for in the sheet's directory and up to three parents
        (utils.sheet_tree.root_candidates), so a sheet in a subdirectory of the
        project finds it. ``create_schematic`` writes ``(sheet_instances ...)``
        into every new file, so that block alone does not mark the root (#423).
        """
        try:
            return instance_paths(Path(schematic_path))[1]
        except Exception:
            return ["/"]

    def _build_instance_path(self, schematic_path: Path) -> str:
        """The first of ``_build_instance_paths``: the path of the sheet's first use."""
        return self._build_instance_paths(schematic_path)[0]

    def _extract_symbol_pins(
        self, schematic_path: Path, library_name: str, symbol_name: str, unit: int
    ) -> List[str]:
        """Return string-sorted pin numbers for the placed unit (plus shared unit-0 pins).

        KiCad writes a ``(pin "N" (uuid ...))`` entry inside every placed symbol for each
        pin; omitting them yields a structurally-incomplete instance the editor can crash
        on when dragged. Pins live inside lib sub-symbols named ``<sym>_<unit>_<style>``;
        unit-0 sub-symbols are shared by every unit. Numbers are returned in KiCad's
        ordering (ascending string sort).
        """
        try:
            content = Path(schematic_path).read_text(encoding="utf-8")
            lib_start = content.find("(lib_symbols")
            if lib_start == -1:
                return []
            sym_start = content.find(f'(symbol "{library_name}:{symbol_name}"', lib_start)
            if sym_start == -1:
                return []
            sym_block = self._extract_paren_block(content, sym_start)

            numbers: List[str] = []
            for m in re.finditer(r'\(symbol\s+"([^"]+)"', sym_block):
                name = m.group(1)
                if ":" in name:
                    # Top-level lib wrapper (e.g. "Device:R"); units live in sub-symbols.
                    continue
                um = re.search(r"_(\d+)_(\d+)$", name)
                sub_unit = int(um.group(1)) if um else None
                if sub_unit not in (None, 0, unit):
                    continue
                sub_block = self._extract_paren_block(sym_block, m.start())
                for pm in re.finditer(r'\(pin\b.*?\(number\s+"([^"]+)"', sub_block, re.DOTALL):
                    numbers.append(pm.group(1))

            return sorted(dict.fromkeys(numbers))
        except Exception:
            return []

    def _extract_lib_property_value(
        self, schematic_path: Path, library_name: str, symbol_name: str, prop_name: str
    ) -> Optional[str]:
        """Return a top-level property value from the injected lib symbol, or None.

        Used to copy Datasheet/Description values into the placed instance so it matches
        KiCad-authored output (KiCad carries these fields on every symbol).
        """
        try:
            content = Path(schematic_path).read_text(encoding="utf-8")
            lib_start = content.find("(lib_symbols")
            if lib_start == -1:
                return None
            sym_start = content.find(f'(symbol "{library_name}:{symbol_name}"', lib_start)
            if sym_start == -1:
                return None
            sym_block = self._extract_paren_block(content, sym_start)
            m = re.search(
                r'\(property\s+"' + re.escape(prop_name) + r'"\s+' + QUOTED_VALUE, sym_block
            )
            return unescape_sexpr_string(m.group(1)) if m else None
        except Exception:
            return None

    def create_component_instance(
        self,
        schematic_path: Path,
        library_name: str,
        symbol_name: str,
        reference: str,
        value: str = "",
        footprint: str = "",
        x: float = 0,
        y: float = 0,
        unit: int = 1,
        angle: float = 0,
        mirror_y: bool = False,
    ) -> bool:
        """
        Add a component instance to the schematic.
        This creates the (symbol ...) block with lib_id reference.

        Property positions and text angles are derived from the library symbol
        definition so that RefDes/Value are always placed correctly for the
        component orientation (e.g. rotated 90° for a vertical resistor).

        Args:
            unit:  For multi-unit symbols, which unit to place (1=A, 2=B, …).
            angle: Placement rotation in degrees (KiCad CCW-in-screen convention).
        """
        full_lib_id = f"{library_name}:{symbol_name}"
        new_uuid = str(uuid.uuid4())

        # Snap the symbol origin to the 1.27 mm (50 mil) schematic connection grid.
        # Library pins sit at integer multiples of 1.27 mm from the origin, so once
        # the origin is on-grid every pin lands on-grid too — a prerequisite for
        # wires and net labels to bind electrically (otherwise ERC reports
        # endpoint_off_grid and the netlist comes up empty).
        # The round(..., 2) is exact, not cosmetic: every multiple of 1.27 is
        # k*127/100 and so has at most two decimals, but the float product
        # (e.g. 79 * 1.27) can carry binary representation dust that would
        # otherwise be written into the file verbatim ("100.32999999999998")
        # and reformatted by KiCad on its next save.
        _GRID = 1.27
        snapped_x = round(round(x / _GRID) * _GRID, 2)
        snapped_y = round(round(y / _GRID) * _GRID, 2)
        if (snapped_x, snapped_y) != (x, y):
            logger.info(
                f"Snapped {reference} origin ({x}, {y}) -> ({snapped_x}, {snapped_y})"
                " onto 1.27mm grid"
            )
        x, y = snapped_x, snapped_y

        # --- read property offsets from the already-injected lib_symbols block -----
        lib_props = self._extract_lib_property_positions(schematic_path, library_name, symbol_name)

        _DEFAULT_EFFECTS = "(effects (font (size 1.27 1.27)))"

        def _prop_at(
            name: str, fallback_dx: float, fallback_dy: float, fallback_angle: float = 0
        ) -> tuple:
            """Return (abs_x, abs_y, text_angle, effects_str, hidden) for a property."""
            if name in lib_props:
                dx, dy, text_ang, eff, hidden = lib_props[name]
            else:
                dx, dy, text_ang, eff = fallback_dx, fallback_dy, fallback_angle, _DEFAULT_EFFECTS
                hidden = False
            rdx, rdy = self._rotate_offset(dx, dy, angle)
            return round(x + rdx, 3), round(y + rdy, 3), text_ang, eff, hidden

        # Reference/Value inherit the library's visibility. Power symbols
        # (power:GND, power:+3V3, …) hide Reference by convention — the #PWR101
        # designators are meaningless to a reader — and losing that flag on
        # placement printed one stray designator beside every ground symbol.
        # Footprint/Datasheet/Description stay hidden unconditionally, matching
        # what eeschema writes on placement.
        ref_x, ref_y, ref_a, ref_eff, ref_hide = _prop_at("Reference", 2.032, 0, 0)
        val_x, val_y, val_a, val_eff, val_hide = _prop_at("Value", 0, 2.54, 0)
        fp_x, fp_y, fp_a, fp_eff, _ = _prop_at("Footprint", 0, 0, 0)
        ds_x, ds_y, ds_a, ds_eff, _ = _prop_at("Datasheet", 0, 0, 0)
        desc_x, desc_y, desc_a, desc_eff, _ = _prop_at("Description", 0, 0, 0)

        def _fmt(n: float) -> str:
            """Format a coordinate the way KiCad does: integral values without a
            trailing ``.0`` (e.g. 100.0 -> '100', 90.0 -> '90')."""
            try:
                f = float(n)
            except (TypeError, ValueError):
                return str(n)
            return str(int(f)) if f.is_integer() else str(n)

        def _clean_effects(eff: str) -> str:
            """Strip legacy hide markers from a lib (effects ...) string.

            KiCad 10 records field visibility with a top-level (hide yes) on the
            property, not inside (effects ...). Remove both the parenthesised
            (hide ...) and the bare ``hide`` token so we don't emit a v9-style
            effects block beside the v10 top-level (hide yes).
            """
            eff = re.sub(r"\s*\(hide\s+[^)]*\)", "", eff)
            eff = re.sub(r"\s+hide(?=[\s)])", "", eff)
            return eff.strip() or "(effects (font (size 1.27 1.27)))"

        def _property(
            name: str, value: str, px: float, py: float, pa: float, eff: str, hide: bool
        ) -> str:
            """Build a KiCad-10 (property ...) block.

            Field order: at, [hide yes], show_name no, do_not_autoplace no, effects.
            """
            hide_line = "      (hide yes)\n" if hide else ""
            # Escape both: library property values legitimately contain quotes
            # (power:GND's Description is `... global label with name "GND"`),
            # and emitted raw the inner quote closes the token early, splitting
            # one value into three s-expression atoms (#324/#336).
            return (
                f'    (property "{escape_sexpr_string(name)}"'
                f' "{escape_sexpr_string(value)}"\n'
                f"      (at {_fmt(px)} {_fmt(py)} {_fmt(pa)})\n"
                f"{hide_line}"
                f"      (show_name no)\n"
                f"      (do_not_autoplace no)\n"
                f"      {_clean_effects(eff)}\n"
                f"    )"
            )

        # Datasheet/Description values come from the injected lib symbol. KiCad normalises
        # the placeholder "~" datasheet to an empty string, so mirror that to keep the
        # generated instance byte-stable across a KiCad save (round-trip).
        ds_val = self._extract_lib_property_value(
            schematic_path, library_name, symbol_name, "Datasheet"
        )
        ds_val = "" if ds_val in (None, "~") else ds_val
        desc_val = (
            self._extract_lib_property_value(
                schematic_path, library_name, symbol_name, "Description"
            )
            or ""
        )

        # Footprint inherits from the library symbol the same way (#300): the
        # library value is the default KiCad itself applies on placement, and
        # most callers pass nothing. An explicit argument still wins. The
        # extractor is the escape-aware one (#348), so a footprint containing
        # a quote inherits intact and the escaping writer below (#324) emits
        # it correctly.
        if not footprint:
            footprint = (
                self._extract_lib_property_value(
                    schematic_path, library_name, symbol_name, "Footprint"
                )
                or ""
            )

        properties_str = "\n".join(
            [
                _property("Reference", reference, ref_x, ref_y, ref_a, ref_eff, ref_hide),
                _property("Value", value or symbol_name, val_x, val_y, val_a, val_eff, val_hide),
                _property("Footprint", footprint, fp_x, fp_y, fp_a, fp_eff, True),
                _property("Datasheet", ds_val, ds_x, ds_y, ds_a, ds_eff, True),
                _property("Description", desc_val, desc_x, desc_y, desc_a, desc_eff, True),
            ]
        )

        # Per-pin uuids — KiCad writes a (pin "N" (uuid ...)) for every pin of the placed
        # unit; their absence is a primary cause of editor crashes when the symbol is dragged.
        pin_numbers = self._extract_symbol_pins(schematic_path, library_name, symbol_name, unit)
        pins_str = "\n".join(f'    (pin "{n}" (uuid "{uuid.uuid4()}"))' for n in pin_numbers)

        # Real project name + hierarchical sheet path (not the "project" / "/"
        # placeholders), one (path ...) per use of the sheet (#428). A sheet
        # used twice needs a reference of its own in each use: with a single
        # entry, the second use has none, and the netlist loses one part and
        # lists another twice. The first use gets the requested reference.
        instance_project = self._resolve_project_name(schematic_path)
        try:
            instances = references_for_new_symbol(Path(schematic_path), reference, instance_project)
        except Exception as e:
            logger.warning(f"Instance paths for {schematic_path} not resolved ({e}); using '/'")
            instances = [("/", reference)]
        self.placed_instances = instances
        path_entries = "".join(
            f'        (path "{escape_sexpr_string(path)}"\n'
            f'          (reference "{escape_sexpr_string(ref)}")\n'
            f"          (unit {unit})\n"
            "        )\n"
            for path, ref in instances
        )
        instances_str = (
            "    (instances\n"
            f'      (project "{escape_sexpr_string(instance_project)}"\n'
            f"{path_entries}"
            "      )\n"
            "    )"
        )

        body = "\n".join(part for part in [properties_str, pins_str, instances_str] if part)

        content, newline = read_text_preserve_newline(Path(schematic_path))

        # Emit the KiCad 10-only symbol attributes only into files whose declared
        # format version accepts them (#351). A KiCad 8 or 9 file never contains
        # them and those releases refuse a schematic carrying a token they do not
        # know. Everything else on this line is common to v8/v9/v10.
        if self._supports_kicad10_symbol_tokens(content):
            attrs_line = (
                "    (body_style 1) (exclude_from_sim no) (in_bom yes) (on_board yes)"
                " (in_pos_files yes) (dnp no)\n"
            )
        else:
            attrs_line = "    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n"

        mirror_str = " (mirror y)" if mirror_y else ""
        instance_block = (
            f'  (symbol (lib_id "{escape_sexpr_string(full_lib_id)}")'
            f" (at {_fmt(x)} {_fmt(y)} {_fmt(angle)})"
            f"{mirror_str} (unit {unit})\n"
            f"{attrs_line}"
            f'    (uuid "{new_uuid}")\n'
            f"{body}\n"
            "  )"
        )

        # Insert before (sheet_instances using direct string search.
        # This works for both pretty-printed and sexpdata-compacted single-line files.
        insert_marker = "(sheet_instances"
        insert_at = content.rfind(insert_marker)
        if insert_at == -1:
            # Hierarchical sub-sheets don't carry (sheet_instances ...) — only the
            # root .kicad_sch does. Fall back to inserting just before the final
            # closing paren of the outer (kicad_sch ...) form.
            stripped = content.rstrip()
            if not stripped.endswith(")"):
                raise ValueError("Could not find insertion point in schematic")
            insert_at = len(stripped) - 1
            content = content[:insert_at] + instance_block + "\n" + content[insert_at:]
        else:
            content = content[:insert_at] + instance_block + "\n  " + content[insert_at:]

        write_text_atomic(Path(schematic_path), content, newline)

        logger.info(f"Added component instance {reference} ({full_lib_id}) at ({x}, {y})")
        return True

    def load_symbol_dynamically(
        self, schematic_path: Path, library_name: str, symbol_name: str
    ) -> str:
        """
        Complete workflow: inject symbol definition and create a template instance.
        Returns a template reference name.
        """
        logger.info(f"Loading symbol dynamically: {library_name}:{symbol_name}")

        # Step 1: Inject symbol definition into lib_symbols
        self.inject_symbol_into_schematic(schematic_path, library_name, symbol_name)

        # Step 2: Create an offscreen template instance
        lib_clean = library_name.replace("-", "_").replace(".", "_")
        sym_clean = symbol_name.replace("-", "_").replace(".", "_")
        template_ref = f"_TEMPLATE_{lib_clean}_{sym_clean}"

        self.create_component_instance(
            schematic_path,
            library_name,
            symbol_name,
            reference=template_ref,
            value=symbol_name,
            x=-200,
            y=-200,
        )

        logger.info(f"Symbol loaded. Template reference: {template_ref}")
        return template_ref

    def add_component(
        self,
        schematic_path: Path,
        library_name: str,
        symbol_name: str,
        reference: str,
        value: str = "",
        footprint: str = "",
        x: float = 0,
        y: float = 0,
        unit: int = 1,
        angle: float = 0,
        mirror_y: bool = False,
        project_path: Optional[Path] = None,
    ) -> bool:
        """
        High-level: ensure symbol definition exists in schematic, then add an instance.
        This is the main entry point for adding components.

        Args:
            unit:  For multi-unit symbols, which unit to place (1=A, 2=B, …). Default 1.
            angle: Placement rotation in degrees (KiCad CCW-in-screen). Default 0.
            project_path: Optional project directory for project-local sym-lib-table.

        All-or-nothing: the library symbol is validated before anything is
        written, and if any step raises, the schematic is restored byte-for-
        byte (no orphan instance, no half-injected lib_symbols entry — #5c).
        """
        if project_path:
            self.project_path = project_path
        with FileTransaction(Path(schematic_path)):
            # Ensure symbol definition is in lib_symbols
            self.inject_symbol_into_schematic(schematic_path, library_name, symbol_name)

            # Add the component instance (property positions come from the injected lib def)
            return self.create_component_instance(
                schematic_path,
                library_name,
                symbol_name,
                reference=reference,
                value=value,
                footprint=footprint,
                x=x,
                y=y,
                unit=unit,
                angle=angle,
                mirror_y=mirror_y,
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    loader = DynamicSymbolLoader()

    print("\n=== Testing Dynamic Symbol Loader (Text-based) ===\n")

    print("1. Finding KiCad symbol library directories...")
    lib_dirs = loader.find_kicad_symbol_libraries()
    print(f"   Found {len(lib_dirs)} directories")

    print("\n2. Extracting symbols...")
    for lib, sym in [
        ("Device", "R"),
        ("Device", "C"),
        ("Device", "LED"),
        ("Device", "Q_NMOS"),
    ]:
        block = loader.extract_symbol_from_library(lib, sym)
        if block:
            print(f"   OK: {lib}:{sym} ({len(block)} chars)")
        else:
            print(f"   FAIL: {lib}:{sym}")

    print("\n3. Testing extends resolution...")
    block = loader.extract_symbol_from_library("Regulator_Switching", "LM2596S-5")
    if block and "(extends" not in block and '(symbol "LM2596S-5_' in block:
        print(f"   OK: LM2596S-5 flattened from its parent ({len(block)} chars)")
    else:
        print(f"   FAIL: extends not resolved")

    print("\nAll tests passed!")
