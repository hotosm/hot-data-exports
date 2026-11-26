import json

import source_geometry

SQUARE = json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]})

CONFIG = """
iso3: NPL
key: t
hdx:
  combine: true
  combined:
    name: hot_test_npl
boundary:
  geom: '{geom}'
  buffer_meters: {buffer}
categories:
  - name: buildings
    osm:
      enabled: true
      filter:
        building: true
"""


def write_config(tmp_path, buffer):
    path = tmp_path / "c.yaml"
    path.write_text(CONFIG.format(geom=SQUARE, buffer=buffer), encoding="utf-8")
    return path


def run(monkeypatch, path, extra=()):
    monkeypatch.setattr(
        "sys.argv", ["source_geometry.py", "--config", str(path), "--name", "n", *extra]
    )
    return source_geometry.main()


def test_a_config_without_a_buffer_is_refused(tmp_path, monkeypatch):
    assert run(monkeypatch, write_config(tmp_path, 0), ["--dry-run"]) == 2


def fail(*args, **kwargs):
    raise AssertionError("must not be called")


def test_a_dry_run_publishes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(source_geometry, "publish", fail)
    assert run(monkeypatch, write_config(tmp_path, 200), ["--dry-run"]) == 0


def test_an_unset_bucket_fails_before_hdx_is_read(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OEX_S3_BUCKET", raising=False)
    monkeypatch.setattr(source_geometry.Dataset, "read_from_hdx", fail)
    assert run(monkeypatch, write_config(tmp_path, 200)) == 1
    assert "bucket" in capsys.readouterr().err


def test_a_geometry_kept_in_a_file_is_measured(tmp_path, monkeypatch, capsys):
    area = tmp_path / "area.geojson"
    area.write_text(SQUARE, encoding="utf-8")
    path = tmp_path / "c.yaml"
    path.write_text(CONFIG.format(geom=area, buffer=200), encoding="utf-8")
    assert run(monkeypatch, path, ["--dry-run"]) == 0
    assert "12308.78 sq km" in capsys.readouterr().out


def test_a_geometry_that_is_not_polygons_is_refused_with_a_message(tmp_path, monkeypatch, capsys):
    point = json.dumps({"type": "Point", "coordinates": [0, 0]})
    path = tmp_path / "c.yaml"
    path.write_text(CONFIG.format(geom=point, buffer=200), encoding="utf-8")
    assert run(monkeypatch, path, ["--dry-run"]) == 2
    assert "only polygons" in capsys.readouterr().err
