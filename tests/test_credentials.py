import json

import pytest
from keyring.errors import PasswordSetError

from work_rescue import credentials
from work_rescue.cli import main
from work_rescue.credentials import CredentialStore, CredentialsUnavailable


class MemorySystemBackend:
    __module__ = "keyring.backends.Windows"
    priority = 5

    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        self.values[service, account] = password

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


@pytest.fixture
def system_store(monkeypatch):
    backend = MemorySystemBackend()
    monkeypatch.setattr(credentials.keyring, "get_keyring", lambda: backend)
    return backend


def test_passwords_are_repository_scoped_and_never_written_to_configuration(tmp_path, system_store):
    one, two = CredentialStore(tmp_path / "one"), CredentialStore(tmp_path / "two")
    one.save("first-private-password")
    assert one.load() == "first-private-password"
    assert two.load() is None
    assert not list(tmp_path.rglob("*"))
    one.forget()
    assert one.load() is None


def test_plaintext_plugin_is_not_a_fallback(tmp_path, monkeypatch):
    class Plaintext(MemorySystemBackend):
        __module__ = "keyrings.alt.file"

    monkeypatch.setattr(credentials.keyring, "get_keyring", lambda: Plaintext())
    with pytest.raises(CredentialsUnavailable, match="明文"):
        CredentialStore(tmp_path).save("do-not-persist")
    assert not list(tmp_path.rglob("*"))


def test_store_error_is_reported_without_silent_fallback(tmp_path, monkeypatch, system_store):
    def fail(*args):
        raise PasswordSetError("unavailable")

    monkeypatch.setattr(system_store, "set_password", fail)
    with pytest.raises(CredentialsUnavailable):
        CredentialStore(tmp_path).save("private")


@pytest.mark.parametrize(
    "backend_method,operation",
    [("get_password", "load"), ("set_password", "save"), ("delete_password", "forget")],
)
def test_native_backend_errors_use_the_manual_fallback(
    tmp_path, monkeypatch, system_store, backend_method, operation
):
    class NativeVaultError(Exception):
        pass

    def fail(*args):
        raise NativeVaultError("native vault unavailable")

    monkeypatch.setattr(system_store, backend_method, fail)
    store = CredentialStore(tmp_path)
    with pytest.raises(CredentialsUnavailable):
        getattr(store, operation)(*(["private"] if operation == "save" else []))


def test_cli_remembers_only_after_successful_repository_unlock(
    engine, monkeypatch, system_store, capsys
):
    monkeypatch.setenv("WORK_RESCUE_PASSWORD", engine.backend.password)
    main(
        [
            "--home",
            str(engine.state.home),
            "--repo",
            str(engine.backend.repository),
            "--restic",
            str(engine.backend.executable),
            "--remember",
            "init",
        ]
    )
    assert json.loads(capsys.readouterr().out)["initialized"]
    assert CredentialStore(engine.backend.repository).load() == engine.backend.password
    for path in engine.state.home.rglob("*.json"):
        assert engine.backend.password not in path.read_text(encoding="utf-8")


def test_cli_unlocks_with_saved_repository_password(engine, monkeypatch, system_store, capsys):
    engine.backend.init()
    CredentialStore(engine.backend.repository).save(engine.backend.password)
    monkeypatch.delenv("WORK_RESCUE_PASSWORD", raising=False)
    main(
        [
            "--home",
            str(engine.state.home),
            "--repo",
            str(engine.backend.repository),
            "--restic",
            str(engine.backend.executable),
            "init",
        ]
    )
    assert json.loads(capsys.readouterr().out)["initialized"]


def test_wrong_confirmation_does_not_initialize_repository(engine, monkeypatch, system_store):
    answers = iter(["first-password", "different-password"])
    monkeypatch.delenv("WORK_RESCUE_PASSWORD", raising=False)
    monkeypatch.setattr("work_rescue.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("work_rescue.cli.getpass.getpass", lambda prompt: next(answers))
    with pytest.raises(SystemExit) as result:
        main(
            [
                "--home",
                str(engine.state.home),
                "--repo",
                str(engine.backend.repository),
                "--restic",
                str(engine.backend.executable),
                "init",
            ]
        )
    assert result.value.code == 1
    assert not (engine.backend.repository / "config").exists()
