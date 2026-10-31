"""The research screen's oracles, on synthetic trees built in `tmp_path` (never a fixture tree)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from bench.research import Question, oracles

N = 8  # modules in the generated tree


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def build(root: Path, consts_in: int = N) -> Path:
    """A tree with 5+ candidates per template (V is thin when `consts_in` is small)."""
    mention = "# mentions: app data lib svc\n"
    files: dict[str, str] = {f"{d}/__init__.py": mention for d in ("lib", "app", "svc", "data")}
    for i in range(N):
        consts = (
            f'RATE_LIMIT_{i} = {i + 1}\nLABEL_TEXT_{i} = "label{i}"\nNEXT_VALUE_{i} = {100 + i}\n'
            if i < consts_in
            else ""
        )
        files[f"lib/mod{i}.py"] = (
            f"{consts}\n\ndef compute_total_{i}(x):\n    return x\n\n\nclass Widget{i}:\n    pass\n"
        )
        tests = "\n".join(f"def test_case_{j}():\n    assert True\n" for j in range(8))
        files[f"tests/test_mod{i}.py"] = (
            f"import lib.mod{i}\n# compute_total_{i}\n\n{tests}\n\n"
            "class TestNested:\n    def test_inner(self):\n        pass\n"
        )
        files[f"app/use{i}.py"] = (
            f"# see compute_total_{i}\nfrom lib.mod0 import Widget0\n"
            if i == 0
            else f"# see compute_total_{i}\n"
        )
        files[f"docs/guide{i}.md"] = (
            f"# Guide {i}\n\n## How the unique topic number {i} works\n\n"
            "## Common shared heading text here\n"
        )
    names = ", ".join(f'"lib.mod{i}"' for i in range(N))
    funcs = ", ".join(f'"compute_total_{i}"' for i in range(N))
    files["tests/test_names.py"] = f"NAMES = [{names}]\nFUNCS = [{funcs}]\n\n" + "\n".join(
        f"def test_name_{j}():\n    assert NAMES\n" for j in range(8)
    )
    files["svc/run.py"] = "import app.use0\nfrom lib.mod1 import Widget1\n"
    files["data/x.py"] = "# lib is not imported here\nfrom app import use0\nimport svc.run\n"
    files["docs/index.md"] = (
        "# Index\n\nSee [guide](guide0.md#top) and [web](https://example.org/a).\n"
    )
    files["docs/notes.md"] = "# Notes\n\n```bash\n# a comment inside code is not a heading\n```\n"
    files["pyproject.toml"] = "[project]\nname = 'demo'\n\n[project.scripts]\n" + "".join(
        f'demo-cmd{i} = "lib.mod{i}:main"\n' for i in range(N)
    )
    lock = '[[package]]\nname = "demo"\nversion = "0.1.0"\nsource = { editable = "." }\n\n'
    lock += "".join(f'[[package]]\nname = "dep-{i}"\nversion = "1.{i}.0"\n\n' for i in range(N))
    files["uv.lock"] = lock
    files["requirements.txt"] = "".join(f"dep_{i}==1.{i}.9\n" for i in range(N))
    write(root, files)
    return root


@pytest.fixture(scope="module")
def big(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("big"))


@pytest.fixture(scope="module")
def pooled(big: Path) -> tuple[dict[str, list[Question]], dict[str, int]]:
    return oracles.pools(big)


def by_id(pool: list[Question], qid: str) -> Question:
    return next(q for q in pool if q.id == qid)


# ---- each template's oracle finds the key and records its decoys -------------------------------


def test_l_finds_the_defining_file_and_records_callers(
    pooled: tuple[dict[str, list[Question]], dict[str, int]],
) -> None:
    q = by_id(pooled[0]["L"], "L-compute_total_3")
    assert q.key == "lib/mod3.py" and q.kind == "path"
    assert q.text == (
        "In which file is the function or class `compute_total_3` defined? "
        "Give the repo-relative path."
    )
    assert q.source_files == ("lib/mod3.py",)
    assert q.decoys == ("app/use3.py", "tests/test_mod3.py", "tests/test_names.py")


LONG = "alpha_long_function_name"


def callers(name: str, count: int) -> dict[str, str]:
    return {f"users/user{i}.py": f"# {name}\n" for i in range(count)}


def l_ids(tree: Path) -> list[str]:
    return [q.id for q in oracles.pools(tree)[0]["L"]]


def test_l_needs_a_long_name_three_other_callers_and_a_file_name_that_does_not_say_it(
    tmp_path: Path,
) -> None:
    write(tmp_path / "ok", {"core.py": f"def {LONG}():\n    pass\n", **callers(LONG, 3)})
    write(tmp_path / "few", {"core.py": f"def {LONG}():\n    pass\n", **callers(LONG, 2)})
    write(
        tmp_path / "short", {"core.py": "def short_name():\n    pass\n", **callers("short_name", 3)}
    )
    write(
        tmp_path / "said",
        {f"{LONG}.py": f"def {LONG}():\n    pass\n", **callers(LONG, 3)},
    )
    assert l_ids(tmp_path / "ok") == [f"L-{LONG}"]
    assert oracles.pools(tmp_path / "ok")[0]["L"][0].decoys == (
        "users/user0.py",
        "users/user1.py",
        "users/user2.py",
    )
    assert l_ids(tmp_path / "few") == l_ids(tmp_path / "short") == l_ids(tmp_path / "said") == []


@pytest.mark.parametrize(
    "extra",
    [
        {"tests/test_x.py": f"def {LONG}():\n    pass\n"},  # defined again in a test
        {"other.py": f"class Box:\n    def {LONG}(self):\n        pass\n"},  # a method
        {"other.py": f"def outer():\n    def {LONG}():\n        pass\n"},  # nested
        {"other.py": f"{LONG} = 3\n"},  # assigned at top level
        {"other.py": f"{LONG}: int = 3\n"},
    ],
)
def test_l_drops_a_name_with_another_definition_or_assignment_anywhere(
    tmp_path: Path, extra: dict[str, str]
) -> None:
    write(tmp_path, {"core.py": f"def {LONG}():\n    pass\n", **callers(LONG, 3), **extra})
    assert l_ids(tmp_path) == []
    assert oracles.pools(tmp_path)[1]["L"] == 0  # a filter, not a disagreement


def test_v_number_and_string_with_neighbour_decoys(
    pooled: tuple[dict[str, list[Question]], dict[str, int]],
) -> None:
    pool = pooled[0]["V"]
    number = by_id(pool, "V-RATE_LIMIT_2")
    assert (number.key, number.kind, number.source_files) == (3, "number", ("lib/mod2.py",))
    assert number.text == (
        "What value does the module-level constant `RATE_LIMIT_2` have in the current code?"
    )
    text = by_id(pool, "V-LABEL_TEXT_2")
    assert (text.key, text.kind) == ("label2", "string")
    assert set(text.decoys) == {"3", "102"}  # the constants on either side


def test_v_skips_constants_assigned_twice(tmp_path: Path) -> None:
    write(tmp_path, {"a.py": "SOME_LIMIT = 1\nSOME_LIMIT = 2\nFLAG_ON = True\n"})
    assert oracles.pools(tmp_path)[0]["V"] == []


def test_c_counts_tests_and_files_with_off_by_one_decoys(
    pooled: tuple[dict[str, list[Question]], dict[str, int]],
) -> None:
    pool = pooled[0]["C"]
    tests = by_id(pool, "C-tests-tests__test_mod4.py")
    assert (tests.key, tests.kind, tests.source_files) == (8, "number", ("tests/test_mod4.py",))
    assert tests.text == (
        "How many functions named `test_*` are defined at module level in `tests/test_mod4.py`?"
    )
    assert tests.decoys == (
        "9",
    )  # the method inside the class counts too when indentation is ignored
    lib = by_id(pool, "C-files-lib")
    assert lib.key == N and len(lib.source_files) == N
    assert lib.text == (
        "How many `.py` files, not counting `__init__.py`, are directly inside `lib/` "
        "(not its subfolders)?"
    )
    assert lib.decoys == (str(N + 1),)  # with __init__.py; the recursive count equals the key


def test_i_finds_importing_tests_and_needs_a_text_only_decoy(
    pooled: tuple[dict[str, list[Question]], dict[str, int]],
) -> None:
    q = by_id(pooled[0]["I"], "I-lib.mod5")
    assert q.key == frozenset({"tests/test_mod5.py"}) and q.kind == "set"
    assert q.text == "Which test files import the module `lib.mod5`? List every path."
    assert q.decoys == ("tests/test_names.py",)


def test_i_drops_a_module_pulled_in_by_from_package_import(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "lib/__init__.py": "",
            "lib/leaf.py": "",
            "tests/test_a.py": "from lib import leaf\n",
            "tests/test_b.py": "import lib.leaf\n",
            "tests/test_c.py": 'NAME = "lib.leaf"\n',
        },
    )
    assert oracles.pools(tmp_path)[0]["I"] == []


def test_a_none_is_the_empty_set_and_one_importer_is_listed(
    pooled: tuple[dict[str, list[Question]], dict[str, int]],
) -> None:
    pool = pooled[0]["A"]
    none = by_id(pool, "A-lib__data")
    assert none.key == frozenset() and none.kind == "set" and none.source_files == ()
    assert none.text == (
        "Does any file under `lib/` import the package `data`? Answer `none`, or list the paths."
    )
    one = by_id(pool, "A-app__lib")
    assert one.key == frozenset({"app/use0.py"}) and one.source_files == ("app/use0.py",)
    assert none.decoys == ("lib/__init__.py",)  # mentions `data` in a comment, imports nothing
    data_lib = by_id(pool, "A-data__lib")
    assert data_lib.key == frozenset()
    assert data_lib.decoys == ("data/__init__.py", "data/x.py")


def test_s_console_script_and_lock_version(
    pooled: tuple[dict[str, list[Question]], dict[str, int]],
) -> None:
    pool = pooled[0]["S"]
    cmd = by_id(pool, "S-cmd-demo-cmd3")
    assert (cmd.key, cmd.kind, cmd.source_files) == ("lib/mod3.py", "path", ("lib/mod3.py",))
    assert cmd.text == "Which file implements the console command `demo-cmd3`?"
    assert cmd.decoys == ("lib.mod3", "lib.mod3:main")
    lock = by_id(pool, "S-lock-dep-2")
    assert (lock.key, lock.kind, lock.source_files) == ("1.2.0", "string", ("uv.lock",))
    assert lock.text == "Which version of `dep-2` does `uv.lock` pin?"
    assert lock.decoys == ("1.2.9",)  # the requirements.txt pin
    assert not any(q.id == "S-lock-demo" for q in pool)  # the project itself is not a dependency


def test_h_heading_lookup_with_link_and_overlap_decoys(
    pooled: tuple[dict[str, list[Question]], dict[str, int]],
) -> None:
    pool = pooled[0]["H"]
    q = next(p for p in pool if p.key == "docs/guide0.md")
    assert q.key == "docs/guide0.md" and q.kind == "path"
    assert q.text == (
        "Which file under `docs/` has the section heading `How the unique topic number 0 works`? "
        "Give the repo-relative path."
    )
    assert q.decoys[0] == "docs/index.md"  # links to the target
    assert "docs/guide1.md" in q.decoys  # shares 2+ words in a heading
    ids = {p.id for p in pool}
    assert not any("Common" in i or "comment" in i for i in ids)  # repeated / inside a fence
    assert not any(i.startswith("H-Guide") for i in ids)  # fewer than four words


# ---- two methods must agree ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "files"),
    [
        (
            "L",
            {
                "a.py": "def planted_function_name():\n    pass\n",
                "b.py": '"""\ndef planted_function_name():\n    pass\n"""\n',
                "c.py": "# planted_function_name\n",
                "d.py": "# planted_function_name\n",
            },
        ),
        ("V", {"a.py": "PLANTED_LIMIT = 3\n", "b.py": '"""\nPLANTED_LIMIT = 3\n"""\n'}),
        (
            "C",
            {
                "tests/test_a.py": "".join(f"def test_{j}():\n    pass\n" for j in range(8))
                + '"""\ndef test_phantom():\n"""\n'
            },
        ),
        (
            "I",
            {
                "lib/m.py": "",
                "tests/test_a.py": "import lib.m\nTEXT = 'lib.m'\n",
                "tests/test_b.py": '"""\nimport lib.m\n"""\n',
            },
        ),
        ("A", {"d/a.py": '"""\nimport pkg\n"""\n', "pkg/__init__.py": ""}),
        ("S", {"pyproject.toml": '[project.scripts]\n"odd cmd" = "m:main"\n', "m.py": ""}),
        (
            "S",
            {
                "uv.lock": '[[package]]\nname = "dep"\nsource = {}\nversion = "1.0"\n',
            },
        ),
        ("H", {"docs/a.md": "# Four words in title ##\n"}),
    ],
)
def test_a_planted_disagreement_drops_the_candidate_and_is_counted(
    tmp_path: Path, template: str, files: dict[str, str]
) -> None:
    write(tmp_path, files)
    pooled, dropped = oracles.pools(tmp_path)
    assert dropped[template] >= 1
    if template == "S":
        assert pooled["S"] == []
    if template in "LVCIH":
        assert [q for q in pooled[template] if "planted" in q.id.lower()] == []
    assert not any("phantom" in str(q) for q in pooled[template])
    assert all(n >= 0 for n in dropped.values())


def test_a_clean_tree_drops_nothing(
    pooled: tuple[dict[str, list[Question]], dict[str, int]],
) -> None:
    assert pooled[1] == dict.fromkeys(oracles.TEMPLATES, 0)


# ---- what is never a source ---------------------------------------------------------------------


def test_answer_key_hidden_and_vendored_areas_are_never_a_source_or_question(
    tmp_path: Path,
) -> None:
    hidden = {
        "docs/eval/answers.md": "# The secret answer key heading words\n",
        "docs/eval/secret.py": "SECRET_LIMIT = 5\n# visible_function_name\ndef secretly(): 1",
        ".hid/x.py": "def hidden_function():\n    pass\n",
        "venv/x.py": "def venv_function():\n    pass\n# visible_function_name\n",
        ".venv/x.py": "def dotvenv_function():\n    pass\n",
        "node_modules/x.py": "def node_function():\n    pass\n",
        "pkg/__pycache__/x.py": "def cached_function():\n    pass\n",
        "pkg/seen.py": "def visible_function_name():\n    pass\n# secretly_defined hidden_function",
        "pkg/c0.py": "# visible_function_name\n",
        "pkg/c1.py": "# visible_function_name\n",
        "pkg/c2.py": "# visible_function_name\n",
    }
    write(tmp_path, hidden)
    pooled, _ = oracles.pools(tmp_path)
    assert [q.id for q in pooled["L"]] == ["L-visible_function_name"]
    assert pooled["L"][0].decoys == ("pkg/c0.py", "pkg/c1.py", "pkg/c2.py")
    assert pooled["V"] == [] and pooled["H"] == []
    blob = repr([q for qs in pooled.values() for q in qs])
    for word in ("secret", "hidden_function", "venv_function", "node_function", "cached"):
        assert word not in blob


# ---- draw and order -----------------------------------------------------------------------------


def test_draw_is_deterministic_per_seed_and_differs_across_seeds(big: Path) -> None:
    first = oracles.draw(big, 1)
    assert oracles.draw(big, 1) == first
    assert [q.id for q in oracles.draw(big, 2)] != [q.id for q in first]
    counts = Counter(q.template for q in first)
    assert counts == dict.fromkeys(oracles.TEMPLATES, 5)
    assert len({q.id for q in first}) == 35
    assert [q.template for q in first] == sorted(
        (q.template for q in first), key=oracles.TEMPLATES.index
    )


@pytest.mark.parametrize("seed", range(8))
def test_draw_prefers_questions_with_decoys(big: Path, seed: int) -> None:
    pool = oracles.pools(big)[0]["C"]
    assert any(not q.decoys for q in pool) and sum(bool(q.decoys) for q in pool) >= 4
    drawn = oracles.draw(big, seed)
    for template in oracles.TEMPLATES:
        assert sum(bool(q.decoys) for q in drawn if q.template == template) >= 4


def test_draw_gives_a_answer_set_half_exactly_one_importer(big: Path) -> None:
    for seed in range(5):
        a = [q for q in oracles.draw(big, seed) if q.template == "A"]
        assert sum(isinstance(q.key, frozenset) and len(q.key) == 1 for q in a) == 3
        assert any(q.key == frozenset() for q in a)


def test_draw_per_template_changes_the_size(big: Path) -> None:
    assert len(oracles.draw(big, 3, per_template=2)) == 14


def test_draw_raises_when_a_pool_is_thin(tmp_path: Path) -> None:
    write(tmp_path, {"a.py": "def only_function():\n    pass\n"})
    with pytest.raises(ValueError, match="template L"):
        oracles.draw(tmp_path, 1)


def test_draw_lets_v_return_fewer(tmp_path: Path) -> None:
    thin = build(tmp_path, consts_in=1)
    pool_v = oracles.pools(thin)[0]["V"]
    assert 0 < len(pool_v) < 5
    drawn = oracles.draw(thin, 1)
    assert sum(q.template == "V" for q in drawn) == len(pool_v)
    assert sum(q.template == "L" for q in drawn) == 5


def test_order_differs_per_repeat_and_is_reproducible(big: Path) -> None:
    questions = oracles.draw(big, 1)
    first = oracles.order(questions, 7, 0)
    assert oracles.order(questions, 7, 0) == first
    assert [q.id for q in oracles.order(questions, 7, 1)] != [q.id for q in first]
    assert [q.id for q in oracles.order(questions, 8, 0)] != [q.id for q in first]
    assert sorted(q.id for q in first) == sorted(q.id for q in questions)
    assert [q.id for q in questions] == [q.id for q in oracles.draw(big, 1)]  # input untouched


def test_ids_are_stable_unique_and_prefixed_by_template(big: Path) -> None:
    again = oracles.pools(big)[0]
    for template, pool in again.items():
        assert [q.id for q in pool] == [q.id for q in oracles.pools(big)[0][template]]
        assert len({q.id for q in pool}) == len(pool)
        assert all(q.id.startswith(f"{template}-") and q.template == template for q in pool)
        assert all(" " not in q.id and "/" not in q.id for q in pool)


def test_reading_a_tree_writes_nothing_into_it(big: Path) -> None:
    def snapshot() -> list[tuple[str, int, int]]:
        return sorted(
            (str(p.relative_to(big)), p.stat().st_size, p.stat().st_mtime_ns)
            for p in big.rglob("*")
        )

    before = snapshot()
    oracles.draw(big, 4)
    assert snapshot() == before
    assert not list(big.rglob("__pycache__"))


def test_v_drops_a_constant_that_is_rebound_anywhere(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "a.py": "KEEP_ME = 1\nGLOBALLY = 2\nAUGMENTED = 3\nSHADOWED = 4\nLOOPED = 5\n",
            "b.py": "def f():\n    global GLOBALLY\n    return GLOBALLY\n",
            "c.py": "def g(x):\n    AUGMENTED += 1\n",
            "d.py": "def h():\n    SHADOWED = 9\n    return SHADOWED\n",
            "e.py": "def k(xs):\n    for LOOPED in xs:\n        pass\n",
        },
    )
    assert [q.id for q in oracles.pools(tmp_path)[0]["V"]] == ["V-KEEP_ME"]


def test_a_decoys_are_files_naming_the_package_only_in_a_comment_or_string(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "d/a.py": "# talks about pkg\n",
            "d/b.py": 'TEXT = "see pkg"\n',
            "d/c.py": "pkg = 1\n",
            "d/e.py": "print(pkg)  # pkg\n",
            "d/f.py": "x = 1\n",
        },
    )
    q = next(q for q in oracles.pools(tmp_path)[0]["A"] if q.id == "A-d__pkg")
    assert q.key == frozenset() and q.decoys == ("d/a.py", "d/b.py")


def test_h_drops_titles_with_markup_but_keeps_intraword_underscores(tmp_path: Path) -> None:
    titles = {
        "plain": "Plain title with four words",
        "snake": "Uses snake_case in its title",
        "star": "A *starred* title with words",
        "code": "A `code` title with words",
        "link": "A [link](x.md) title with words",
        "html": "An <b>html</b> title with words",
        "under": "An _emphasised_ title with words",
    }
    write(tmp_path, {f"docs/{k}.md": f"# {t}\n" for k, t in titles.items()})
    keys = {q.key for q in oracles.pools(tmp_path)[0]["H"]}
    assert keys == {"docs/plain.md", "docs/snake.md"}


@pytest.mark.parametrize("count", [5, 3])
def test_draw_raises_when_too_few_questions_have_decoys(tmp_path: Path, count: int) -> None:
    thin = build(tmp_path, consts_in=0)
    write(thin, {f"extra/solo{i}.py": f"SOLO_VALUE_{i} = {i}\n" for i in range(count)})
    assert len(oracles.pools(thin)[0]["V"]) == count
    with pytest.raises(ValueError, match="template V"):
        oracles.draw(thin, 1)
