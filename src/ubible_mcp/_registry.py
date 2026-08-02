from __future__ import annotations

import gzip
import sys
from datetime import date
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryFile
from zipfile import ZipFile

import msgspec
import orjson
import requests
from loguru import logger as log
from pygments.lexer import default

_ = log.configure(handlers=[{"sink": sys.stderr, "level": "INFO"}])  # ty:ignore[invalid-argument-type]


class RegistryInfo(msgspec.Struct):
    version: int


class RegistryHost(msgspec.Struct):
    alias: str
    path: str
    priority: int
    weight: int


class LocalizedDescription(msgspec.Struct):
    lang: str = msgspec.field(name="lng")
    desc: str = msgspec.field(name="des")


class RegistryDownload(msgspec.Struct, kw_only=True):
    abbrev: str = msgspec.field(name="abr")
    language: str | None = msgspec.field(default=None, name="lng")
    description: str | None = msgspec.field(default=None, name="des")
    localized_descriptions: list[LocalizedDescription] = msgspec.field(
        default_factory=list, name="lds"
    )
    file_name: str = msgspec.field(name="fil")
    update_date: date = msgspec.field(name="upd")
    changelog: str = msgspec.field(name="cmt")

    size: str | None = msgspec.field(default=None, name="size")
    bkq: str | None = msgspec.field(default=None)
    url: list[str]
    hidden: bool | None = msgspec.field(default=None, name="hid")

    def make_urls(self, hosts: list[RegistryHost]) -> list[str]:
        urls: list[str] = []
        for u in self.url:
            pref, u = u.split("}")
            pref = pref.lstrip("{")
            for host in hosts:
                if pref == host.alias:
                    urls.append(host.path % u)
        return urls


class RegistryData(msgspec.Struct):
    version: int
    hosts: list[RegistryHost]
    downloads: list[RegistryDownload]
    registries: list[RegistrySource]


class RegistrySource(msgspec.Struct):
    url: str
    info_url: str
    priority: int
    test: bool = msgspec.field(default=False)

    def get_info(self) -> RegistryInfo | None:
        try:
            resp = requests.get(self.info_url)
            resp.raise_for_status()
        except Exception:
            log.exception("Cannot load registry info: {}", self.info_url)
        else:
            text = resp.content.decode("utf-8-sig")
            return msgspec.json.decode(text, type=RegistryInfo)

    def get_data(self) -> str | None:
        hash_name = sha256(self.url.encode()).hexdigest()[:16] + ".json.gz"
        if (Path(".") / hash_name).exists():
            with open(hash_name, "rb") as f_gz, gzip.GzipFile(fileobj=f_gz) as f:
                data = f.read()
                return data.decode("utf-8-sig")
        try:
            resp = requests.get(self.url, stream=True)
            resp.raise_for_status()
        except Exception:
            log.exception("Cannot load registry data: {}", self.url)
            return
        try:
            with TemporaryFile() as zip_file:
                while chunk := resp.raw.read(8192):
                    zip_file.write(chunk)
                zip_file.flush()

                with (
                    ZipFile(zip_file) as file,
                    file.open("registry.json") as reg_data,
                    open(hash_name, "wb") as f_gz,
                    gzip.GzipFile(fileobj=f_gz, mode="wb") as f,
                ):
                    while chunk := reg_data.read(8192):
                        f.write(chunk)
            return self.get_data()
        except Exception:
            log.exception("Cannot load registry data: {}", self.url)


DEFAULT_REGISTRY_SOURCES: list[RegistrySource] = [
    RegistrySource(
        "https://dl.dropbox.com/s/keg0ptkkalux5fi/registry.zip",
        "https://dl.dropbox.com/s/1odi2f2tyn1oqyx/registry_info.json",
        False,
        2,
    ),
    RegistrySource(
        "http://mybible.zone/repository/registry/registry.zip",
        "http://mybible.zone/repository/registry/registry_info.json",
        False,
        1,
    ),
    RegistrySource(
        "http://mybible.infoo.pro/registry.zip",
        "http://mybible.infoo.pro/registry_info.json",
        False,
        1,
    ),
    RegistrySource(
        "http://mybible.i-t.kz/registry.zip",
        "http://mybible.i-t.kz/registry_info.json",
        False,
        1,
    ),
    RegistrySource(
        "http://myb.1gb.ru/registry.zip",
        "http://myb.1gb.ru/registry_info.json",
        False,
        1,
    ),
    RegistrySource(
        "http://mph4.ru/registry.zip", "http://mph4.ru/registry_info.json", False, 1
    ),
    RegistrySource(
        "http://mybible.zone/repository/registry/registry_test.zip",
        "http://mybible.zone/repository/registry/registry_test_info.json",
        True,
        1,
    ),
]


class ModuleModel(msgspec.Struct):
    download_url: str
    file_name: str
    language_code: str
    description: str
    update_date: str
    update_info: str


class RegistryModel(msgspec.Struct):
    url: str
    file_name: str
    description: str

    modules: list[ModuleModel]
