"""Private dependencies and ordinary CLI behavior through the development entry point."""

from pathlib import Path

from tests.support.development import DevelopmentCheckout


def test_development_setup_reuses_dependencies_and_normal_user_directory(tmp_path):
    checkout = DevelopmentCheckout(tmp_path)

    checkout.run("--help")
    checkout.run("setup", "--url", "https://controller.example:7443")
    checkout.run("controller", "check")

    assert len(checkout.syncs) == 1
    config = Path(checkout.env["HOME"]) / ".quirkbench/config.toml"
    assert "controller.example" in config.read_text()
    assert not (checkout.root / ".quirkbench").exists()


def test_dependency_changes_refresh_private_environment(tmp_path):
    checkout = DevelopmentCheckout(tmp_path)
    checkout.run("--version")
    with (checkout.root / "uv.lock").open("a") as lock:
        lock.write("\n# Changed dependency selection\n")

    result = checkout.run("--version")
    checkout.run("--version")

    assert "Quirkbench" in result.stdout
    assert len(checkout.syncs) == 2


def test_distrobox_forwards_once_and_source_edits_need_no_dependency_setup(tmp_path):
    checkout = DevelopmentCheckout(tmp_path)
    checkout.env.pop("QUIRKBENCH_DEV_ON_HOST")
    checkout.env["CONTAINER_ID"] = "development-box"
    checkout.run("--version")
    source = checkout.root / "src/quirkbench/__init__.py"
    source.write_text(source.read_text() + "\n__version__ = '9.9.9'\n")

    result = checkout.run("--version")

    assert "9.9.9" in result.stdout and len(checkout.syncs) == 1
    assert (tmp_path / "forwarded").read_text().splitlines() == ["forwarded", "forwarded"]
