from pathlib import Path
from zipfile import ZipFile

wheel = next(Path("dist").glob("eventmem-1.0.0-*.whl"))
with ZipFile(wheel) as archive:
    names = archive.namelist()
    for required in [
        "eventmem/web/index.html",
        "eventmem/core/engine.py",
        "eventmem/sdk/__init__.pyi",
        "eventmem/sdk/py.typed",
    ]:
        assert required in names, required
    assert any(
        n.startswith("eventmem/web/assets/") and n.endswith(".js") for n in names
    )
    assert not any("/.env" in n or "/memory.sqlite3" in n for n in names)
print("Wheel contains core, typed SDK and console assets")
