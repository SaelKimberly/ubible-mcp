from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Generator
from zipfile import ZipFile

import msgspec
import requests
from loguru import logger as log
from rapidfuzz import fuzz

from ._registry import (
    DEFAULT_REGISTRY_SOURCES,
    RegistryData,
    RegistryDownload,
    RegistrySource,
)

ACTUAL_REGISTRY_SOURCES: list[RegistrySource] | None = None


class GetModuleResult(msgspec.Struct):
    abr: str
    reg: str | None
    mod: RegistryDownload | None
    url: list[str] | None
    sim: dict[str, float]

    def download(self):
        if self.mod is None or self.reg is None or not self.url:
            log.warning(
                f"Module with abbrev {self.abr!r} not found. Similar: {self.sim!r}"
            )
            return
        folder = Path(".") / f"reg.{self.reg}"
        path = folder / self.mod.file_name
        zip_path = path.with_suffix(".zip")

        if not folder.exists():
            folder.mkdir()
        elif not folder.is_dir():
            raise RuntimeError("Cannot create registry folder: {str(folder)!r}")

        if path.exists():
            if not path.is_dir():
                raise RuntimeError(
                    "Cannot create module folder: {str(path)!r} (file found)"
                )
            if (path / "checksum.txt").exists():
                zip_path.unlink(missing_ok=True)
                return

        for url in self.url:
            try:
                resp = requests.get(url, stream=True)
                resp.raise_for_status()

                hashsum = sha256()
                with zip_path.open("wb") as file:
                    while chunk := resp.raw.read(8192):
                        file.write(chunk)
                        hashsum.update(chunk)
            except Exception:
                log.exception("Failed to download from {url!r}")
                continue

            path.mkdir(exist_ok=True)
            with ZipFile(zip_path) as zip_file:
                zip_file.extractall(path)

            (path / "checksum.txt").write_text(hashsum.hexdigest())
            zip_path.unlink()

            break


def get_module_by_abbrev(abbrev: str) -> GetModuleResult:
    global ACTUAL_REGISTRY_SOURCES
    sources = (
        DEFAULT_REGISTRY_SOURCES
        if ACTUAL_REGISTRY_SOURCES is None
        else ACTUAL_REGISTRY_SOURCES
    )
    maybe: dict[str, float] = {}
    for reg in sources:
        if data := reg.get_data():
            data = msgspec.json.decode(data, type=RegistryData)
            if ACTUAL_REGISTRY_SOURCES is None:
                ACTUAL_REGISTRY_SOURCES = data.registries
        else:
            continue

        for d in data.downloads:
            if d.abbrev == abbrev:
                return GetModuleResult(
                    abr=abbrev,
                    reg=reg.name,
                    mod=d,
                    url=d.make_urls(data.hosts),
                    sim=maybe,
                )
            elif (ratio := fuzz.ratio(d.abbrev, abbrev)) > 50.0:
                maybe[d.abbrev] = ratio

    if maybe:
        maybe = dict(sorted(list(maybe.items()), key=lambda x: x[1])[-10:][::-1])
        log.warning(f"Module with abbrev {abbrev!r} not found. Similar: {maybe!r}")
    else:
        log.warning(
            f"Module with abbrev {abbrev!r} not found. No similar abbrev in registry"
        )
    return GetModuleResult(abr=abbrev, reg=None, mod=None, url=None, sim=maybe)


class VerseSelector(msgspec.Struct):
    book: str
    c_start: int
    c_final: int
    v_start: int
    v_final: int | None

    _VERSE_EXPR = re.compile(
        r"(?P<book>\w+)[ \.](?P<cs>[1-9]\d*):(?P<vs>[1-9]\d*)(?:-(?:(?P<cf>[1-9]\d*):)?(?P<vf>[1-9]\d*))?"
    )

    @classmethod
    def parse(cls, expr: str) -> VerseSelector | None:
        if m := cls._VERSE_EXPR.match(expr.strip()):
            return VerseSelector(
                book=m.group("book"),
                c_start=int(m.group("cs")),
                c_final=int(m.group("cf") or m.group("cs")),
                v_start=int(vs := m.group("vs")),
                v_final=int(vf)
                if (vf := m.group("vf")) is not None and vs != vf
                else None,
            )
        else:
            log.warning("Cannot parse verse selector: {expr!r}.")
            return None

    def to_sql(self) -> tuple[str, str, list[int]] | None:
        book = self.book
        args: list[int]
        match self.c_start, self.c_final, self.v_start, self.v_final:
            # simplest: single verse
            case int(cs), int(cf), int(vs), None if cs == cf:
                stmt = "chapter = ? and verse = ?"
                args = [cs, vs]
                spec = f"{book} {cs}:{vs}"
            # verse range in one chapter
            case int(cs), int(cf), int(vs), int(vf) if cs == cf:
                stmt = "chapter = ? and verse >= ? and verse <= ?"
                args = [cs, vs, vf]
                spec = f"{book} {cs}:{vs}-{vf}"
            # exact two chapters
            case int(cs), int(cf), int(vs), int(vf) if cs + 1 == cf:
                stmt = "((chapter = ? and verse >= ?) or (chapter = ? and verse <= ?))"
                args = [cs, vs, cf, vf]
                spec = f"{book} {cs}:{vs}-{cf}:{vf}"
            # more then two chapters
            case int(cs), int(cf), int(vs), int(vf) if cs + 1 < cf:
                stmt = "((chapter = ? and verse >= ?) or (chapter > ? and chapter < ?) or (chapter = ? and verse <= ?))"
                args = [cs, vs, cs, cf, cf, vf]
                spec = f"{book} {cs}:{vs}-{cf}:{vf}"
            case int(cs), int(cf), int(vs), None if cs != cf:
                log.warning(
                    "When final chapter specified, final verse also must be specified"
                )
                return None
        return spec, stmt, args


_STRONG_RE_X = re.compile(r"(\s*<S>(?:[^<]+)</S>)+\s*")


class ModuleSession(msgspec.Struct):
    reg: str
    mod: RegistryDownload

    books: dict[str, int] = msgspec.field(default_factory=dict)
    book_short: dict[int, str] = msgspec.field(default_factory=dict)
    chaps: dict[int, int] = msgspec.field(default_factory=dict)
    ch_vs: dict[tuple[int, int], int] = msgspec.field(default_factory=dict)

    @contextmanager
    def open_module(self) -> Generator[sqlite3.Connection]:
        path = Path(".") / f"reg.{self.reg}" / self.mod.file_name / ".sqlite3"
        with sqlite3.connect(path) as conn:
            yield conn

    @classmethod
    def create(cls, abbrev: str) -> ModuleSession | None:
        mod = get_module_by_abbrev(abbrev)
        if mod.reg is None or mod.mod is None:
            return None
        mod.download()
        slf = cls(mod.reg, mod.mod)
        with slf.open_module() as conn:
            cur = conn.execute("select * from books")
            for _, book_idx, short, long in cur:
                slf.books[short] = book_idx
                slf.books[long] = book_idx
                slf.book_short[book_idx] = short
            # Get chapters per book
            cur = conn.execute(
                "select book_number, count(chapter) as chaps from verses group by book_number"
            )
            slf.chaps.update(dict(cur))
            # Get verses per book and chapter
            cur = conn.execute(
                "select book_number, chapter, count(verse) as vs from verses group by book_number, chapter"
            )
            slf.ch_vs.update({(b, c): v for b, c, v in cur})
        return slf

    def _validate_selector(self, sel: VerseSelector) -> VerseSelector | None:
        if (book_idx := self.books.get(sel.book)) is None:
            maybe = {}
            for book in self.books:
                if (ratio := fuzz.ratio(book, sel.book)) > 0.5:
                    maybe[book] = ratio
            if maybe:
                maybe = dict(
                    sorted(list(maybe.items()), key=lambda x: x[1])[-10:][::-1]
                )
                log.warning(
                    f"Book {book!r} not found in module {self.mod.abbrev!r}. Similar: {maybe!r}"
                )
            else:
                log.warning(
                    f"Book {book!r} not found in module {self.mod.abbrev!r}. No similar book in module."
                )
            return None
        else:
            sel.book = self.book_short[book_idx]

        chapter_cnt = self.chaps[book_idx]
        if sel.c_start > chapter_cnt:
            log.warning(
                f"Book {sel.book!r} in module {self.mod.abbrev!r} contains {chapter_cnt} chapters"
                f" (min: 1, max: {chapter_cnt}). Chapter {sel.c_start} not found."
            )
            return None
        if sel.c_final > chapter_cnt:
            log.warning(
                f"Book {sel.book!r} in module {self.mod.abbrev!r} contains {chapter_cnt} chapters"
                f" (min: 1, max: {chapter_cnt}). Chapter {sel.c_start} not found."
            )
            return None

        start_verse_cnt = self.ch_vs[book_idx, sel.c_start]
        if sel.v_start > start_verse_cnt:
            log.warning(
                f"Chapter {sel.c_start} of {sel.book!r} book in {self.mod.abbrev!r} module contains only {start_verse_cnt} verses"
                f" (min: 1, max: {start_verse_cnt}). Verse {sel.v_start} not found."
            )
            return None

        if sel.v_final is not None:
            final_verse_cnt = self.ch_vs[book_idx, sel.c_final]
            if sel.v_final > final_verse_cnt:
                log.warning(
                    f"Chapter {sel.c_final} of {sel.book!r} book in {self.mod.abbrev!r} module contains only {final_verse_cnt} verses"
                    f" (min: 1, max: {final_verse_cnt}). Verse {sel.v_final} not found."
                )
                return None

        return sel

    def _get_area_raw(
        self, sel: VerseSelector
    ) -> tuple[str, dict[tuple[int, int], str]] | None:
        if (xsel := self._validate_selector(sel)) is None:
            return None
        else:
            sel = xsel
        if (stm := sel.to_sql()) is None:
            return None
        spec, stmt, args = stm
        book_number = self.books[sel.book]

        with self.open_module() as conn:
            cur = conn.execute(
                f"select chapter, verse, text from verses where book_number = ? and {stmt};",
                [book_number, *args],
            )
            return spec, {(c, v): text for c, v, text in cur}

    def get_verses(self, expr: str | VerseSelector) -> str | None:
        if isinstance(expr, str):
            if (sel := VerseSelector.parse(expr)) is None:
                return None
            if (sel := self._validate_selector(sel)) is None:
                return None
        else:
            sel = expr
        if (spec_raw_area := self._get_area_raw(sel)) is not None:
            spec, raw_area = spec_raw_area
            per_chapter: defaultdict[int, list[tuple[int, str]]] = defaultdict(list)

            for (c, v), txt in raw_area.items():
                per_chapter[c].append((v, txt))
            per_chapter_bounds = {
                c: (min(v for v, _ in t), max(v for v, _ in t))
                for c, t in per_chapter.items()
            }
            chunks = []

            for c, (c_min, c_max) in per_chapter_bounds.items():
                chunks.append(
                    f"## {sel.book} {c}:{c_min}"
                    if c_min == c_max
                    else f"{sel.book} {c}:{c_min}-{c_max}"
                )
                for verse, text in per_chapter[c]:
                    text = (
                        _STRONG_RE_X.sub(" ", text)
                        .replace("<pb/>", "")
                        .replace("<t>", "")
                        .replace("</t>", "")
                        .replace("<i>", "*")
                        .replace("</i>", "*")
                    )
                    chunks.append(f"{verse}. {text}")
                chunks.append("---")

            return "\n".join(chunks)
