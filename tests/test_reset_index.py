from pathlib import Path

import pytest

import reset_index_db

ROOT = Path(__file__).resolve().parent.parent


def test_reset_refuses_without_confirmation(monkeypatch, capsys):
    monkeypatch.delenv("CONFIRM", raising=False)
    monkeypatch.setattr(
        reset_index_db, "get_connection", lambda: pytest.fail("must not connect")
    )

    assert reset_index_db.main() == 1
    assert "CONFIRM=RESET" in capsys.readouterr().err


def test_reset_truncates_all_index_tables(monkeypatch):
    executed = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql):
            executed.append(sql)

    monkeypatch.setenv("CONFIRM", "RESET")
    monkeypatch.setattr(reset_index_db, "get_connection", Conn)

    assert reset_index_db.main() == 0
    assert len(executed) == 1
    for table in ("chunks", "documents", "source_connections"):
        assert table in executed[0]


def test_reset_workflow_requires_confirmation_and_removes_firewall_rule():
    text = (ROOT / ".github/workflows/reset-index-azure.yml").read_text(
        encoding="utf-8"
    )

    assert "inputs.confirm == 'RESET'" in text
    assert "environment: azure-test" in text
    assert "::add-mask::$url" in text
    assert "if: ${{ always()" in text
    assert "firewall-rule delete" in text
    assert text.index("actions/start-postgres") < text.index("firewall-rule create")
