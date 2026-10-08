from PIL import Image

from departureboard.cli import load_env_file, main


def test_demo_writes_preview_without_any_keys(tmp_path):
    out = tmp_path / "demo.png"
    assert main(["demo", "--png", str(out)]) == 0
    assert Image.open(out).size == (250, 122)


def test_load_env_file_parses_assignments(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text('# comment\nRDM_API_KEY=abc\n\nTAPI_APP_ID = "id"\nTAPI_APP_KEY=\'k=1\'\n')
    assert load_env_file(path) == {"RDM_API_KEY": "abc", "TAPI_APP_ID": "id", "TAPI_APP_KEY": "k=1"}


def test_run_reports_config_errors_without_traceback(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    code = main(["run", "--config", str(config), "--secrets", str(tmp_path / "missing.env")])
    assert code == 2
    assert "RDM_API_KEY" in capsys.readouterr().err
