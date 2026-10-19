"""Runtime Lite import extraction and resolution, one fixture per import style."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lemoncrow_client.lite import imports
from lemoncrow_client.lite.imports import ImportResolver, extract_imports, imported_names


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _resolve_all(root: Path, files: dict[str, str]) -> tuple[dict[str, set[str]], dict[tuple[str, str], str]]:
    """Per importing file: the files it links to, and each (file, module)'s status."""

    _write(root, files)
    resolver = ImportResolver(root, files)
    links: dict[str, set[str]] = {}
    statuses: dict[tuple[str, str], str] = {}
    for path, text in files.items():
        for _path, module, imported, _line in extract_imports(path, text):
            resolution = resolver.resolve(path, module, imported)
            statuses[(path, module)] = resolution.status
            links.setdefault(path, set()).update(resolution.targets)
    return links, statuses


def _modules(path: str, text: str) -> list[tuple[str, str]]:
    return [(module, imported) for _path, module, imported, _line in extract_imports(path, text)]


# --------------------------------------------------------------------------- #
# Python                                                                      #
# --------------------------------------------------------------------------- #

_PYTHON = {
    "pyproject.toml": "[project]\nname = 'shop'\n",
    "src/shop/__init__.py": "",
    "src/shop/pricing.py": "def total():\n    return 1\n",
    "src/shop/models/__init__.py": "",
    "src/shop/models/item.py": "class Item:\n    pass\n",
    "src/shop/models/view.py": "from ..pricing import total\nfrom ..... import too_far\n",
    "src/shop/cart.py": (
        "from . import pricing\n"
        "from .models.item import Item\n"
        "from .models import (\n"
        "    item,  # the submodule\n"
        ")\n"
        "import shop.pricing as p, shop.models\n"
        "from plugins.extra import hook\n"
        "import plugins\n"
        "import requests, os.path\n"
        "from typing import Any\n"
        "from shop.absent import nothing\n"
    ),
    # A namespace package: no __init__.py anywhere on the way.
    "src/plugins/extra/hook.py": "def run():\n    pass\n",
    "scripts/tool.py": "import helper\n",
    "scripts/helper.py": "",
    # Inside a package an absolute ``import json`` is the standard library, not the sibling.
    "src/shop/codec.py": "import json\n",
    "src/shop/json.py": "",
    # A local adapter package named like the distribution it adapts.
    "tools/pyproject.toml": "[project]\nname = 'tools'\n",
    "tools/harbor/__init__.py": "",
    "tools/harbor/agent.py": "from harbor.agents.base import Agent\n",
}


def test_python_rows_cover_relative_alias_comma_and_parenthesised_imports() -> None:
    assert _modules("src/shop/cart.py", _PYTHON["src/shop/cart.py"]) == [
        (".", "pricing"),
        (".models.item", "Item"),
        (".models", "item"),
        ("shop.pricing", ""),
        ("shop.models", ""),
        ("plugins.extra", "hook"),
        ("plugins", ""),
        ("requests", ""),
        ("os.path", ""),
        ("typing", "Any"),
        ("shop.absent", "nothing"),
    ]
    assert _modules("a.py", "from x import a, \\\n    b as c\n") == [("x", "a, b as c")]
    lines = [row[3] for row in extract_imports("src/shop/cart.py", _PYTHON["src/shop/cart.py"])]
    assert lines[:4] == [1, 2, 3, 6]


def test_python_imports_resolve_relative_src_layout_and_namespace_packages(tmp_path: Path) -> None:
    links, statuses = _resolve_all(tmp_path, _PYTHON)

    assert links["src/shop/cart.py"] == {
        "src/shop/pricing.py",
        "src/shop/models/item.py",
        "src/shop/models/__init__.py",
        "src/plugins/extra/hook.py",
    }
    assert links["src/shop/models/view.py"] == {"src/shop/pricing.py"}
    assert links["scripts/tool.py"] == {"scripts/helper.py"}
    assert statuses[("src/shop/cart.py", "plugins")] == "local"
    # Above the repository root, and a module of a local package that is not there.
    assert statuses[("src/shop/models/view.py", ".....")] == "missing"
    assert statuses[("src/shop/cart.py", "shop.absent")] == "missing"


def test_python_external_modules_stay_unresolved_and_never_missing(tmp_path: Path) -> None:
    _links, statuses = _resolve_all(tmp_path, _PYTHON)

    for module in ("requests", "os.path", "typing"):
        assert statuses[("src/shop/cart.py", module)] == "external"
    assert statuses[("src/shop/codec.py", "json")] == "external"
    assert statuses[("tools/harbor/agent.py", "harbor.agents.base")] == "external"


def test_python_relative_depth_is_counted_from_the_importing_package(tmp_path: Path) -> None:
    files = {
        "a/__init__.py": "",
        "a/x.py": "",
        "a/b/__init__.py": "",
        "a/b/x.py": "",
        "a/b/c/__init__.py": "",
        "a/b/c/x.py": "",
        "a/b/c/m.py": "from .x import f\nfrom ..x import g\nfrom ...x import h\nfrom .... import top\n",
        "top.py": "",
    }
    _write(tmp_path, files)
    resolver = ImportResolver(tmp_path, files)

    def target(module: str, imported: str) -> set[str]:
        return set(resolver.resolve("a/b/c/m.py", module, imported).targets)

    assert target(".x", "f") == {"a/b/c/x.py"}
    assert target("..x", "g") == {"a/b/x.py"}
    assert target("...x", "h") == {"a/x.py"}
    assert target("....", "top") == {"top.py"}
    assert resolver.resolve("a/b/c/m.py", ".....", "top").status == "missing"


def test_python_unresolved_relative_import_is_missing_never_external(tmp_path: Path) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/sub/__init__.py": "",
        "pkg/sub/m.py": "from .gone import x\nfrom ..absent.deep import y\nfrom . import gone\n",
    }
    _links, statuses = _resolve_all(tmp_path, files)

    # A relative import always names this repository: inside the package bounds it is missing.
    assert statuses[("pkg/sub/m.py", ".gone")] == "missing"
    assert statuses[("pkg/sub/m.py", "..absent.deep")] == "missing"
    # ``from . import gone`` may name an attribute of pkg/sub/__init__.py, which is here.
    assert statuses[("pkg/sub/m.py", ".")] == "resolved"


# --------------------------------------------------------------------------- #
# JavaScript / TypeScript                                                     #
# --------------------------------------------------------------------------- #

_MAIN_TS = """\
import React, { useState } from "react";
import {
  widget,
  type Widget,
} from "./widgets";
import "./polyfill";
import type { Config } from "@app/config";
export * from "./reexport.js";
export { helper as h } from "~utils";
const lazy = await import("./lazy.mjs");
const legacy = require(
  "./legacy"
);
import { secret } from "#internal/secret";
import config from "#config";
import { Button } from "@acme/ui";
import { Card } from "@acme/ui/card";
import { api } from "@acme/api";
import fp from "lodash/fp";
import "./styles.css";
import logo from "./missing.svg";
import gone from "./does-not-exist";
// import commented from "./commented";
const text = "import quoted from './quoted'";
"""

_JS = {
    "package.json": json.dumps(
        {
            "name": "web",
            "private": True,
            "workspaces": ["packages/*"],
            "imports": {
                "#internal/*": "./src/internal/*.ts",
                "#config": {"import": "./src/config.mjs", "default": "./src/config.cjs"},
            },
        }
    ),
    "tsconfig.base.json": (
        "{\n"
        "  // shared compiler options\n"
        '  "compilerOptions": {\n'
        '    "baseUrl": ".",\n'
        '    "paths": {"@app/*": ["src/app/*"], "~utils": ["src/utils/index.ts"],},\n'
        "  },\n"
        "}\n"
    ),
    "tsconfig.json": '{\n  /* the app */\n  "extends": "./tsconfig.base.json",\n  "include": ["src"],\n}\n',
    "src/main.ts": _MAIN_TS,
    "src/widgets/index.tsx": "export const widget = 1;\n",
    "src/polyfill.js": "",
    "src/app/config.ts": "export type Config = {};\n",
    "src/reexport.ts": "export const r = 1;\n",
    "src/utils/index.ts": "export const helper = 1;\n",
    "src/lazy.mts": "export default 1;\n",
    "src/legacy.cjs": "module.exports = {};\n",
    "src/internal/secret.ts": "export const secret = 1;\n",
    "src/config.mjs": "export default {};\n",
    "src/styles.css": "body {}\n",
    "packages/ui/package.json": json.dumps(
        {
            "name": "@acme/ui",
            "exports": {
                ".": {"types": "./dist/index.d.ts", "import": "./dist/index.js"},
                "./*": "./src/*.tsx",
            },
        }
    ),
    "packages/ui/src/index.ts": "export const Button = 1;\n",
    "packages/ui/src/card.tsx": "export const Card = 1;\n",
    "packages/api/package.json": json.dumps({"name": "@acme/api", "main": "lib/index.js"}),
    "packages/api/src/index.ts": "export const api = 1;\n",
}


def test_javascript_rows_cover_multiline_side_effect_type_reexport_dynamic_and_require() -> None:
    rows = extract_imports("src/main.ts", _MAIN_TS)
    assert [(module, imported) for _path, module, imported, _line in rows] == [
        ("react", "React, useState"),
        ("./widgets", "widget, Widget"),
        ("./polyfill", ""),
        ("@app/config", "Config"),
        ("./reexport.js", "*"),
        ("~utils", "helper"),
        ("./lazy.mjs", ""),
        ("./legacy", ""),
        ("#internal/secret", "secret"),
        ("#config", "config"),
        ("@acme/ui", "Button"),
        ("@acme/ui/card", "Card"),
        ("@acme/api", "api"),
        ("lodash/fp", "fp"),
        ("./styles.css", ""),
        ("./missing.svg", "logo"),
        ("./does-not-exist", "gone"),
    ]
    assert [row[3] for row in rows[:3]] == [1, 2, 6]
    assert _modules("a.mjs", 'export * as ns from "./ns";\nexport type { T } from "./t";\n') == [
        ("./ns", "*"),
        ("./t", "T"),
    ]


def test_javascript_imports_resolve_aliases_package_imports_and_workspaces(tmp_path: Path) -> None:
    links, statuses = _resolve_all(tmp_path, _JS)

    assert links["src/main.ts"] == {
        "src/widgets/index.tsx",
        "src/polyfill.js",
        "src/app/config.ts",
        "src/reexport.ts",
        "src/utils/index.ts",
        "src/lazy.mts",
        "src/legacy.cjs",
        "src/internal/secret.ts",
        "src/config.mjs",
        "packages/ui/src/index.ts",
        "packages/ui/src/card.tsx",
        "packages/api/src/index.ts",
        "src/styles.css",
    }
    assert statuses[("src/main.ts", "./does-not-exist")] == "missing"
    assert statuses[("src/main.ts", "./missing.svg")] == "skipped"


def test_javascript_external_packages_stay_unresolved_and_never_missing(tmp_path: Path) -> None:
    _links, statuses = _resolve_all(tmp_path, _JS)

    assert statuses[("src/main.ts", "react")] == "external"
    assert statuses[("src/main.ts", "lodash/fp")] == "external"


def test_javascript_aliases_and_paths_into_installed_packages_stay_external(tmp_path: Path) -> None:
    files = {
        "site/tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@theme/*": ["./src/theme/*"]}}}',
        "site/src/theme/Footer.tsx": "",
        "site/src/page.tsx": 'import Footer from "@theme/Footer";\nimport Navbar from "@theme/Navbar";\n',
        "site/scripts/check.cjs": 'const { test } = require("../node_modules/@playwright/test");\n',
    }
    links, statuses = _resolve_all(tmp_path, files)

    assert links["site/src/page.tsx"] == {"site/src/theme/Footer.tsx"}
    # Not swizzled here: TypeScript falls back to the installed theme package.
    assert statuses[("site/src/page.tsx", "@theme/Navbar")] == "external"
    assert statuses[("site/scripts/check.cjs", "../node_modules/@playwright/test")] == "external"


@pytest.mark.parametrize("extension", [".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"])
def test_javascript_extension_inference_and_index_files(tmp_path: Path, extension: str) -> None:
    files = {
        "app/main.js": 'import a from "./lib/mod";\nimport b from "./lib/dir";\n',
        f"app/lib/mod{extension}": "",
        f"app/lib/dir/index{extension}": "",
    }
    links, _statuses = _resolve_all(tmp_path, files)

    assert links["app/main.js"] == {f"app/lib/mod{extension}", f"app/lib/dir/index{extension}"}


def test_typescript_esm_specifiers_name_their_sources(tmp_path: Path) -> None:
    files = {
        "src/a.ts": 'import { b } from "./b.js";\nimport c from "./c.jsx";\nimport d from "./d.mjs";\n'
        'import e from "./e.cjs";\nimport f from "./f.js";\n',
        "src/b.ts": "",
        "src/c.tsx": "",
        "src/d.mts": "",
        "src/e.cts": "",
        "src/f.js": "",
    }
    links, _statuses = _resolve_all(tmp_path, files)

    assert links["src/a.ts"] == {"src/b.ts", "src/c.tsx", "src/d.mts", "src/e.cts", "src/f.js"}


def test_tsconfig_paths_resolve_from_the_config_that_defines_them(tmp_path: Path) -> None:
    files = {
        "apps/web/jsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}',
        "apps/web/src/page.jsx": 'import Nav from "@/nav";\nimport x from "@/absent";\n',
        "apps/web/src/nav.jsx": "",
    }
    links, statuses = _resolve_all(tmp_path, files)

    assert links["apps/web/src/page.jsx"] == {"apps/web/src/nav.jsx"}
    # An alias that matches but names no file is this repository's, so missing.
    assert statuses[("apps/web/src/page.jsx", "@/absent")] == "missing"


def test_workspace_package_built_entry_maps_back_to_its_source(tmp_path: Path) -> None:
    files = {
        "packages/sdk/package.json": json.dumps(
            {"name": "@acme/sdk", "exports": {".": "./dist/client.js", "./types": "./types/shapes.d.ts"}}
        ),
        "packages/sdk/src/client.ts": "",
        "packages/sdk/src/shapes.ts": "",
        "packages/sdk/src/index.ts": "",
        "apps/cli/main.ts": 'import { client } from "@acme/sdk";\nimport type { Shape } from "@acme/sdk/types";\n',
    }
    links, _statuses = _resolve_all(tmp_path, files)

    # dist/client.js and types/shapes.d.ts are build output of src/client.ts and src/shapes.ts.
    assert links["apps/cli/main.ts"] == {"packages/sdk/src/client.ts", "packages/sdk/src/shapes.ts"}


def test_workspace_package_without_exports_falls_back_to_its_source_index(tmp_path: Path) -> None:
    files = {
        "libs/core/package.json": json.dumps({"name": "core", "version": "1.0.0"}),
        "libs/core/src/index.ts": "",
        "libs/core/src/deep/thing.ts": "",
        "apps/cli/main.ts": 'import core from "core";\nimport thing from "core/deep/thing";\n',
        "node_modules/core/package.json": json.dumps({"name": "core"}),
    }
    links, _statuses = _resolve_all(tmp_path, files)

    assert links["apps/cli/main.ts"] == {"libs/core/src/index.ts", "libs/core/src/deep/thing.ts"}


def test_paths_outside_the_repository_are_never_targets_or_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _write(
        tmp_path / "outside",
        {"lib.ts": "export const x = 1;\n", "package.json": json.dumps({"name": "evil", "main": "lib.js"})},
    )
    files = {
        "tsconfig.json": json.dumps({"compilerOptions": {"paths": {"@ext/*": ["../outside/*"]}}}),
        "pkg/package.json": json.dumps({"name": "pkg", "main": "../../outside/lib.js"}),
        "src/app.ts": "",
    }
    _write(root, files)
    read: list[Path] = []
    real_load = imports._load_config

    def recording(absolute: Path) -> object:
        read.append(absolute.resolve())
        return real_load(absolute)

    monkeypatch.setattr(imports, "_load_config", recording)
    # A file list naming paths outside the repository, as a buggy caller could pass.
    resolver = ImportResolver(root, [*files, "../outside/lib.ts", "../outside/package.json", "/etc/hosts"])

    for spec in ("@ext/lib", "../../outside/lib", "pkg", "evil"):
        assert resolver.resolve("src/app.ts", spec).targets == {}, spec
    assert resolver.resolve("src/app.ts", "evil").status == "external"
    assert read and all(path.is_relative_to(root.resolve()) for path in read)


def test_vendored_node_modules_packages_are_never_workspace_packages(tmp_path: Path) -> None:
    # A committed node_modules is indexed like any tracked file, so it reaches the resolver.
    files = {
        "node_modules/react/package.json": json.dumps({"name": "react", "main": "index.js"}),
        "node_modules/react/index.js": "module.exports = {};\n",
        # Shallower than the workspace package of the same name, so it would win the name.
        "node_modules/core/package.json": json.dumps({"name": "core", "main": "index.js"}),
        "node_modules/core/index.js": "",
        "packages/libs/core/package.json": json.dumps({"name": "core"}),
        "packages/libs/core/src/index.ts": "",
        "apps/web/main.ts": 'import React from "react";\nimport core from "core";\n',
    }
    links, statuses = _resolve_all(tmp_path, files)

    assert statuses[("apps/web/main.ts", "react")] == "external"
    assert links["apps/web/main.ts"] == {"packages/libs/core/src/index.ts"}


# --------------------------------------------------------------------------- #
# Go                                                                          #
# --------------------------------------------------------------------------- #

_GO = {
    "go.mod": "module example.com/shop\n\ngo 1.22\n",
    "cmd/api/main.go": (
        "package main\n\n"
        "import (\n"
        '\t"fmt"\n'
        '\tstore "example.com/shop/internal/store" // the store\n'
        '\t_ "example.com/shop/internal/none"\n'
        ")\n\n"
        'import "example.com/shop/tools/gen"\n\n'
        "func main() { fmt.Println(store.X) }\n"
    ),
    "internal/store/store.go": "package store\n",
    "internal/store/cache.go": "package store\n",
    "internal/store/store_test.go": "package store\n",
    "tools/go.mod": "module example.com/shop/tools\n",
    "tools/gen/gen.go": "package gen\n",
}


def test_go_import_blocks_resolve_through_go_mod(tmp_path: Path) -> None:
    assert _modules("cmd/api/main.go", _GO["cmd/api/main.go"]) == [
        ("fmt", ""),
        ("example.com/shop/internal/store", ""),
        ("example.com/shop/internal/none", ""),
        ("example.com/shop/tools/gen", ""),
    ]
    links, statuses = _resolve_all(tmp_path, _GO)

    # The longest module path wins: tools/gen lives in the nested tools module.
    assert links["cmd/api/main.go"] == {"internal/store/store.go", "internal/store/cache.go", "tools/gen/gen.go"}
    assert statuses[("cmd/api/main.go", "example.com/shop/internal/none")] == "missing"


def test_go_standard_library_and_modules_stay_external(tmp_path: Path) -> None:
    files = {**_GO, "cmd/api/extra.go": 'package main\n\nimport "github.com/acme/lib"\n'}
    _links, statuses = _resolve_all(tmp_path, files)

    assert statuses[("cmd/api/main.go", "fmt")] == "external"
    assert statuses[("cmd/api/extra.go", "github.com/acme/lib")] == "external"


# --------------------------------------------------------------------------- #
# Rust                                                                        #
# --------------------------------------------------------------------------- #

_RUST = {
    "Cargo.toml": '[package]\nname = "shop-core"\nversion = "0.1.0"\n',
    "src/lib.rs": (
        "pub mod cart;\n"
        "mod pricing;\n"
        "use crate::cart::Cart;\n"
        "use std::collections::HashMap;\n"
        "use serde::Serialize;\n"
    ),
    "src/cart.rs": (
        "mod line;\n"
        "use super::pricing::{total, Discount};\n"
        "use self::line::Line;\n"
        "use crate::{\n    pricing::rate,\n    cart::line::Line as Other,\n};\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "    use super::super::pricing::total;\n"
        "    const BRACE: char = '{';\n"
        "}\n"
    ),
    "src/cart/line.rs": "pub struct Line;\n",
    "src/pricing/mod.rs": "pub fn total() {}\n",
    "crates/report/Cargo.toml": '[package]\nname = "report"\n',
    "crates/report/src/lib.rs": "use shop_core::cart::Cart;\nuse report_helpers::X;\n",
}


def test_rust_rows_cover_groups_modules_and_inline_test_modules() -> None:
    assert _modules("src/cart.rs", _RUST["src/cart.rs"]) == [
        ("self::line", "mod"),
        ("super::pricing::", "total, Discount"),
        ("self::line::Line", ""),
        ("crate::", "pricing::rate, cart::line::Line"),
        ("self::", ""),
        ("super::pricing::total", ""),
    ]


def test_rust_use_and_mod_resolve_within_the_crate_and_workspace(tmp_path: Path) -> None:
    links, statuses = _resolve_all(tmp_path, _RUST)

    assert links["src/lib.rs"] == {"src/cart.rs", "src/pricing/mod.rs"}
    assert links["src/cart.rs"] == {"src/cart/line.rs", "src/pricing/mod.rs"}
    assert links["crates/report/src/lib.rs"] == {"src/cart.rs"}
    # ``use super::*`` in an inline test module names its own file.
    assert statuses[("src/cart.rs", "self::")] == "local"


def test_rust_std_and_dependencies_stay_external(tmp_path: Path) -> None:
    _links, statuses = _resolve_all(tmp_path, _RUST)

    assert statuses[("src/lib.rs", "std::collections::HashMap")] == "external"
    assert statuses[("src/lib.rs", "serde::Serialize")] == "external"
    assert statuses[("crates/report/src/lib.rs", "report_helpers::X")] == "external"


def test_rust_super_cannot_climb_above_the_crate_root(tmp_path: Path) -> None:
    files = {**_RUST, "src/lib.rs": "use super::nothing;\n"}
    _links, statuses = _resolve_all(tmp_path, files)

    assert statuses[("src/lib.rs", "super::nothing")] == "missing"


def test_rust_files_outside_any_cargo_package_are_never_missing(tmp_path: Path) -> None:
    _links, statuses = _resolve_all(tmp_path, {"fixtures/sample.rs": "use self::*;\nuse crate::x::Y;\n"})

    assert statuses[("fixtures/sample.rs", "self::")] == "local"
    assert statuses[("fixtures/sample.rs", "crate::x::Y")] == "local"


# --------------------------------------------------------------------------- #
# Java / Kotlin                                                               #
# --------------------------------------------------------------------------- #

_JVM = {
    "src/main/java/com/acme/shop/Cart.java": (
        "package com.acme.shop;\n\n"
        "import com.acme.shop.model.Item;\n"
        "import static com.acme.shop.util.Money.add;\n"
        "import com.acme.shop.service.*;\n"
        "import java.util.List;\n"
    ),
    "src/main/java/com/acme/shop/model/Item.java": "package com.acme.shop.model;\n",
    "src/main/java/com/acme/shop/util/Money.java": "package com.acme.shop.util;\n",
    "src/main/java/com/acme/shop/service/Orders.java": "package com.acme.shop.service;\n",
    "src/main/java/com/acme/shop/service/Billing.kt": "package com.acme.shop.service\n",
    "src/main/kotlin/com/acme/shop/Report.kt": (
        "package com.acme.shop\n\nimport com.acme.shop.model.Item\nimport kotlinx.coroutines.flow.Flow as F\n"
    ),
}


def test_java_and_kotlin_imports_resolve_by_package_path(tmp_path: Path) -> None:
    assert _modules("x/Cart.java", _JVM["src/main/java/com/acme/shop/Cart.java"]) == [
        ("com.acme.shop.model.Item", ""),
        ("com.acme.shop.util.Money.add", "static"),
        ("com.acme.shop.service.*", ""),
        ("java.util.List", ""),
    ]
    links, _statuses = _resolve_all(tmp_path, _JVM)

    assert links["src/main/java/com/acme/shop/Cart.java"] == {
        "src/main/java/com/acme/shop/model/Item.java",
        "src/main/java/com/acme/shop/util/Money.java",
        "src/main/java/com/acme/shop/service/Orders.java",
        "src/main/java/com/acme/shop/service/Billing.kt",
    }
    assert links["src/main/kotlin/com/acme/shop/Report.kt"] == {"src/main/java/com/acme/shop/model/Item.java"}


def test_java_and_kotlin_library_imports_stay_external(tmp_path: Path) -> None:
    _links, statuses = _resolve_all(tmp_path, _JVM)

    assert statuses[("src/main/java/com/acme/shop/Cart.java", "java.util.List")] == "external"
    assert statuses[("src/main/kotlin/com/acme/shop/Report.kt", "kotlinx.coroutines.flow.Flow")] == "external"


# --------------------------------------------------------------------------- #
# Other files and names                                                       #
# --------------------------------------------------------------------------- #


def test_files_without_a_reader_keep_the_original_line_patterns(tmp_path: Path) -> None:
    text = 'import os\nfrom a import b\nconst x = require("y")\n'
    assert _modules("notes.md", text) == [("os", ""), ("a", "b"), ("y", "")]
    _write(tmp_path, {"notes.md": text})
    assert ImportResolver(tmp_path, ["notes.md"]).resolve("notes.md", "os").status == "skipped"


def test_imported_names_drop_aliases_paths_and_markers() -> None:
    assert imported_names("a, b as c, (d)") == ["a", "b", "d"]
    assert imported_names("pricing::rate, cart::Line, self") == ["rate", "Line"]
    assert imported_names("*") == []
    assert imported_names("mod") == []


# --------------------------------------------------------------------------- #
# Bounds                                                                      #
# --------------------------------------------------------------------------- #


def _python_statement(name_line: int) -> str:
    """A parenthesised import whose only name sits on statement line ``name_line``."""

    return "from m import (\n" + "\n" * (name_line - 2) + "last,\n)\nimport after\n"


def test_python_statement_line_bound_at_and_past() -> None:
    bound = imports._MAX_STATEMENT_LINES
    assert _modules("a.py", _python_statement(bound)) == [("m", "last"), ("after", "")]
    # Past the bound the names are cut; the module and later statements remain.
    assert _modules("a.py", _python_statement(bound + 1)) == [("m", ""), ("after", "")]


def test_javascript_clause_bound_at_and_past() -> None:
    bound = imports._MAX_CLAUSE_CHARS

    def source(clause_chars: int) -> str:
        # The clause is ``{a`` + padding + ``} `` before ``from``.
        return "import {a" + " " * (clause_chars - 4) + '} from "./x";\n'

    assert _modules("a.ts", source(bound)) == [("./x", "a")]
    assert _modules("a.ts", source(bound + 1)) == []


def test_javascript_specifier_bound_at_and_past() -> None:
    bound = imports._MAX_SPECIFIER_CHARS
    at = "./" + "a" * (bound - 2)
    past = "./" + "a" * (bound - 1)
    assert _modules("a.ts", f'import "{at}";\n') == [(at, "")]
    assert _modules("a.ts", f'import "{past}";\n') == []


def test_rust_use_group_bound_at_and_past() -> None:
    bound = imports._MAX_CLAUSE_CHARS

    def source(group_chars: int) -> str:
        # The bounded part is ``B`` + padding + ``}`` after the opening brace.
        return "use crate::a::{B" + " " * (group_chars - 2) + "};\n"

    assert _modules("src/lib.rs", source(bound)) == [("crate::a::", "B")]
    assert _modules("src/lib.rs", source(bound + 1)) == []


def _go_package(tmp_path: Path, count: int) -> ImportResolver:
    files = {"go.mod": "module example.com/big\n"}
    files.update({f"pkg/f{index:03d}.go": "package pkg\n" for index in range(count)})
    _write(tmp_path, files)
    return ImportResolver(tmp_path, files)


def test_go_package_file_bound_at_and_past(tmp_path: Path) -> None:
    bound = imports._MAX_PACKAGE_FILES
    at = _go_package(tmp_path / "at", bound).resolve("main.go", "example.com/big/pkg")
    assert (len(at.targets), at.truncated) == (bound, False)
    past = _go_package(tmp_path / "past", bound + 1).resolve("main.go", "example.com/big/pkg")
    assert (len(past.targets), past.truncated) == (bound, True)


def test_jvm_wildcard_file_bound_at_and_past(tmp_path: Path) -> None:
    bound = imports._MAX_PACKAGE_FILES
    for name, count, truncated in (("at", bound, False), ("past", bound + 1, True)):
        files = {f"src/com/acme/C{index:03d}.java": "" for index in range(count)}
        _write(tmp_path / name, files)
        resolution = ImportResolver(tmp_path / name, files).resolve("Main.java", "com.acme.*")
        assert (len(resolution.targets), resolution.truncated) == (bound, truncated)


def test_configuration_file_bound_at_and_past(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imports, "_MAX_CONFIG_FILES", 3)
    for name, count, truncated in (("at", 3, False), ("past", 4, True)):
        files = {f"packages/p{index}/package.json": json.dumps({"name": f"p{index}"}) for index in range(count)}
        files.update({f"packages/p{index}/src/index.ts": "" for index in range(count)})
        _write(tmp_path / name, files)
        resolver = ImportResolver(tmp_path / name, files)
        last = resolver.resolve("app.ts", f"p{count - 1}")
        assert resolver.truncated is truncated
        assert last.truncated is truncated
        # Past the bound the last package is not read, so it reads as external.
        assert last.status == ("external" if truncated else "resolved")


def test_configuration_size_bound_at_and_past(tmp_path: Path) -> None:
    bound = imports._MAX_CONFIG_BYTES
    for name, size, truncated in (("at", bound, False), ("past", bound + 1, True)):
        body = json.dumps({"name": "big"})
        files = {"big/package.json": body + " " * (size - len(body)), "big/src/index.ts": ""}
        _write(tmp_path / name, files)
        assert (tmp_path / name / "big/package.json").stat().st_size == size
        resolver = ImportResolver(tmp_path / name, files)
        resolution = resolver.resolve("app.ts", "big")
        assert (resolution.status, resolver.truncated) == ("external" if truncated else "resolved", truncated)


def _tsconfig_chain(root: Path, hops: int) -> ImportResolver:
    """tsconfig.json extends c1 ... extends c<hops>, which alone defines ``paths``."""

    files = {"src/target.ts": "", "src/app.ts": ""}
    files["tsconfig.json"] = json.dumps({"extends": "./configs/c1.json"})
    for hop in range(1, hops):
        files[f"configs/c{hop}.json"] = json.dumps({"extends": f"./c{hop + 1}.json"})
    files[f"configs/c{hops}.json"] = json.dumps({"compilerOptions": {"paths": {"@t": ["../src/target.ts"]}}})
    _write(root, files)
    return ImportResolver(root, files)


def test_tsconfig_extends_bound_at_and_past(tmp_path: Path) -> None:
    bound = imports._MAX_TSCONFIG_DEPTH
    at = _tsconfig_chain(tmp_path / "at", bound)
    assert set(at.resolve("src/app.ts", "@t").targets) == {"src/target.ts"}
    assert at.truncated is False
    past = _tsconfig_chain(tmp_path / "past", bound + 1)
    assert past.resolve("src/app.ts", "@t").status == "external"
    assert past.truncated is True


def test_tsconfig_extends_cycle_ends(tmp_path: Path) -> None:
    files = {
        "tsconfig.json": json.dumps({"extends": "./b.json"}),
        "b.json": json.dumps({"extends": "./tsconfig.json"}),
        "src/app.ts": "",
    }
    _write(tmp_path, files)
    assert ImportResolver(tmp_path, files).resolve("src/app.ts", "react").status == "external"


def test_configuration_is_read_once_per_resolver_and_cached_by_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {
        "packages/ui/package.json": json.dumps({"name": "ui"}),
        "packages/ui/src/index.ts": "",
        "tsconfig.json": json.dumps({"compilerOptions": {"baseUrl": "."}}),
    }
    _write(tmp_path, files)
    reads: list[str] = []
    real_parse = imports._parse_config

    def counting(name: str, text: str) -> object:
        reads.append(name)
        return real_parse(name, text)

    monkeypatch.setattr(imports, "_parse_config", counting)
    imports._CONFIG_CACHE.clear()
    resolver = ImportResolver(tmp_path, files)
    for _ in range(5):
        assert resolver.resolve("src/a.ts", "ui").status == "resolved"
    assert sorted(reads) == ["package.json", "tsconfig.json"]
    ImportResolver(tmp_path, files).resolve("src/a.ts", "ui")
    assert len(reads) == 2  # Unchanged files come from the cache.
    (tmp_path / "packages/ui/package.json").write_text(json.dumps({"name": "ui-renamed"}), encoding="utf-8")
    assert ImportResolver(tmp_path, files).resolve("src/a.ts", "ui-renamed").status == "resolved"
    assert len(reads) == 3


def test_configuration_cache_bound_at_and_past(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imports, "_MAX_CONFIG_CACHE", 2)
    imports._CONFIG_CACHE.clear()
    for index in range(3):
        path = tmp_path / f"c{index}" / "package.json"
        path.parent.mkdir()
        path.write_text("{}", encoding="utf-8")
        imports._load_config(path)
        assert len(imports._CONFIG_CACHE) == min(index + 1, 2)
    assert str(tmp_path / "c0" / "package.json") not in imports._CONFIG_CACHE
