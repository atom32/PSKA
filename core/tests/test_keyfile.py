from pathlib import Path

from pska_core.keyfile import read_api_key_file


def test_read_api_key_file_supports_json(tmp_path: Path) -> None:
    path = tmp_path / "api_key.txt"
    path.write_text(
        """
{
  "api_key": "sk-test",
  "model": "deepseek-v4-flash",
  "base_url": "https://api.deepseek.com",
  "service_token": "local-service-token"
}
""".strip(),
        encoding="utf-8",
    )

    config = read_api_key_file(path)

    assert config.api_key == "sk-test"
    assert config.model == "deepseek-v4-flash"
    assert config.base_url == "https://api.deepseek.com"
    assert config.service_token == "local-service-token"


def test_read_api_key_file_keeps_legacy_line_format(tmp_path: Path) -> None:
    path = tmp_path / "api_key.txt"
    path.write_text("sk-test\ndeepseek-v4-flash\nhttps://api.deepseek.com\n", encoding="utf-8")

    config = read_api_key_file(path)

    assert config.api_key == "sk-test"
    assert config.model == "deepseek-v4-flash"
    assert config.base_url == "https://api.deepseek.com"
    assert config.service_token == ""
