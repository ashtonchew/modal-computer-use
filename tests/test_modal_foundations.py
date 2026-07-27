from __future__ import annotations

import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from modal_computer_use.benchmarks.provenance import benchmark_provenance
from modal_computer_use.config import ComputerConfig, ImageConfig, NetworkConfig
from modal_computer_use.image import (
    DESKTOP_APT_PACKAGES,
    _named_image_recipe,
    _published_named_image_identities,
    named_image,
    named_image_name,
    publish_named_images,
)
from modal_computer_use.sandbox import (
    modal_billing_report,
    run_modal_benchmark_function_once,
)

REVISION = "0123456789abcdef0123456789abcdef01234567"


def test_desktop_image_omits_unused_xsel_package() -> None:
    assert "xclip" in DESKTOP_APT_PACKAGES
    assert "xsel" not in DESKTOP_APT_PACKAGES


def test_network_config_uses_current_modal_names_and_legacy_aliases() -> None:
    current = NetworkConfig(
        outbound_cidr_allowlist=["10.0.0.0/8"],
        outbound_domain_allowlist=["api.openai.com", "*.github.com"],
        inbound_cidr_allowlist=["203.0.113.0/24"],
    )
    legacy = NetworkConfig(cidr_allowlist=["10.0.0.0/8"])

    assert current.outbound_cidr_allowlist == ["10.0.0.0/8"]
    assert current.outbound_domain_allowlist == ["api.openai.com", "*.github.com"]
    assert current.inbound_cidr_allowlist == ["203.0.113.0/24"]
    assert legacy.outbound_cidr_allowlist == ["10.0.0.0/8"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"outbound_cidr_allowlist": ["not-a-cidr"]}, "valid CIDR"),
        ({"inbound_cidr_allowlist": [""]}, "non-empty"),
        ({"outbound_domain_allowlist": ["  "]}, "non-empty"),
        (
            {"block_all": True, "outbound_cidr_allowlist": []},
            "block_all cannot be combined",
        ),
    ],
)
def test_network_config_rejects_invalid_restrictions(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        NetworkConfig(**kwargs)


def test_block_all_requires_connect_ingress_without_vnc() -> None:
    allowed = ComputerConfig(network={"block_all": True}, ingress="connect")

    assert allowed.network.block_all is True
    with pytest.raises(ValidationError, match="block_all requires connect ingress"):
        ComputerConfig(network={"block_all": True})


def test_named_image_config_requires_full_git_revision_and_browser_kind() -> None:
    config = ComputerConfig(
        resources={"profile": "browser"},
        browser={"kind": "firefox"},
        image={"source": "named", "revision": REVISION},
    )

    assert config.image == ImageConfig(source="named", revision=REVISION)
    with pytest.raises(ValidationError, match="full 40-character Git revision"):
        ImageConfig(source="named", revision="abc123")
    with pytest.raises(ValidationError, match=r"only valid when image\.source is named"):
        ImageConfig(source="inline", environment_name="prod")
    with pytest.raises(ValidationError, match=r"requires browser\.kind"):
        ComputerConfig(
            resources={"profile": "browser"},
            image={"source": "named", "revision": REVISION},
        )
    with pytest.raises(ValidationError, match="require the xfce window manager"):
        ComputerConfig(
            desktop={"window_manager": "openbox"},
            image={"source": "named", "revision": REVISION},
        )


def test_named_browser_image_can_skip_browser_launch() -> None:
    no_browser_launch = ComputerConfig(
        resources={"profile": "browser"},
        browser={"kind": "firefox", "prewarm": False},
        image={"source": "named", "revision": REVISION},
    )
    assert no_browser_launch.browser is not None
    assert no_browser_launch.browser.prewarm is False


def test_modal_benchmark_function_uses_importable_image_module(monkeypatch) -> None:
    function_options: dict[str, object] = {}

    class FakeRemote:
        def remote(self, config: object, *, run_tag: str) -> dict[str, bool]:
            assert config == "config"
            assert run_tag == "run-tag"
            return {"ok": True}

    class FakeApp:
        def __init__(self, name: str) -> None:
            assert name == "app-optimized-provider-runner"

        def function(self, **kwargs: object):
            function_options.update(kwargs)
            return lambda _entrypoint: FakeRemote()

        def run(self):
            return nullcontext()

    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(App=FakeApp))
    monkeypatch.setattr(
        "modal_computer_use.sandbox.named_image",
        lambda **_kwargs: "named-image",
    )

    result = run_modal_benchmark_function_once(
        lambda: {},
        config="config",
        run_tag="run-tag",
        app_name="app",
        region="us-west-2",
        image_revision=REVISION,
        cpu=4.0,
        memory_mib=8192,
        timeout_seconds=900,
    )

    assert result == {"ok": True}
    assert function_options["serialized"] is False
    assert function_options["include_source"] is False
    assert function_options["single_use_containers"] is True


@pytest.mark.parametrize(
    ("profile", "browser", "expected"),
    [
        ("standard", None, f"modal-computer-use-standard:{REVISION}"),
        ("browser", "firefox", f"modal-computer-use-firefox:{REVISION}"),
        ("browser-gpu", "chromium", f"modal-computer-use-chromium:{REVISION}"),
    ],
)
def test_named_image_name_is_browser_specific(
    profile: str,
    browser: str | None,
    expected: str,
) -> None:
    assert named_image_name(revision=REVISION, profile=profile, browser=browser) == expected


def test_named_image_uses_from_name_without_inline_fallback(monkeypatch) -> None:
    calls: list[tuple[str, str | None]] = []

    class FakeImage:
        @classmethod
        def from_name(cls, name: str, *, environment_name: str | None = None) -> object:
            calls.append((name, environment_name))
            return "named-image"

    monkeypatch.setattr(
        "modal_computer_use.image._modal",
        lambda: SimpleNamespace(Image=FakeImage),
    )

    result = named_image(
        revision=REVISION,
        profile="browser",
        browser="firefox",
        environment_name="prod",
    )

    assert result == "named-image"
    assert calls == [(f"modal-computer-use-firefox:{REVISION}", "prod")]


def test_publish_named_images_publishes_all_variants(monkeypatch) -> None:
    published: list[tuple[str, str | None, str]] = []
    monkeypatch.setattr(
        "modal_computer_use.image._published_named_image_identities",
        lambda *, environment_name: set(),
    )

    class FakeRecipe:
        def __init__(self, variant: str) -> None:
            self.variant = variant

        def build(self, app: object) -> FakeRecipe:
            assert app == "image-app"
            return self

        def publish(self, name: str, *, environment_name: str | None = None) -> None:
            published.append((name, environment_name, self.variant))

    monkeypatch.setattr(
        "modal_computer_use.image._modal",
        lambda: SimpleNamespace(
            App=SimpleNamespace(
                lookup=lambda name, create_if_missing, environment_name: "image-app",
            ),
            enable_output=nullcontext,
        ),
    )
    monkeypatch.setattr(
        "modal_computer_use.image._named_image_recipe",
        lambda *, variant, window_manager: FakeRecipe(variant),
    )

    identities = publish_named_images(revision=REVISION, environment_name="prod")

    assert set(identities) == {"standard", "firefox", "chromium"}
    assert {item[0] for item in published} == set(identities.values())
    assert {item[2] for item in published} == {"standard", "firefox", "chromium"}


def test_publish_named_images_keeps_complete_existing_revision(monkeypatch) -> None:
    identities = {
        f"modal-computer-use-{variant}:{REVISION}"
        for variant in ("standard", "firefox", "chromium")
    }
    monkeypatch.setattr(
        "modal_computer_use.image._published_named_image_identities",
        lambda *, environment_name: identities,
    )
    monkeypatch.setattr(
        "modal_computer_use.image._modal",
        lambda: (_ for _ in ()).throw(AssertionError("build started before preflight")),
    )

    result = publish_named_images(revision=REVISION)

    assert set(result.values()) == identities


def test_publish_named_images_resumes_missing_variants_without_overwrite(monkeypatch) -> None:
    published: list[str] = []
    existing = f"modal-computer-use-standard:{REVISION}"

    class FakeRecipe:
        def build(self, app: object) -> FakeRecipe:
            return self

        def publish(self, name: str, *, environment_name: str | None = None) -> None:
            published.append(name)

    monkeypatch.setattr(
        "modal_computer_use.image._published_named_image_identities",
        lambda *, environment_name: {existing},
    )
    monkeypatch.setattr(
        "modal_computer_use.image._modal",
        lambda: SimpleNamespace(
            App=SimpleNamespace(lookup=lambda *args, **kwargs: "image-app"),
            enable_output=nullcontext,
        ),
    )
    monkeypatch.setattr(
        "modal_computer_use.image._named_image_recipe",
        lambda *, variant, window_manager: FakeRecipe(),
    )

    publish_named_images(revision=REVISION)

    assert existing not in published
    assert published == [
        f"modal-computer-use-firefox:{REVISION}",
        f"modal-computer-use-chromium:{REVISION}",
    ]


def test_named_image_recipe_bakes_daemon_source(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakeRecipe:
        def apt_install(self, *packages: str) -> FakeRecipe:
            calls.append(("apt_install", packages))
            return self

        def pip_install_from_pyproject(self, path: str) -> FakeRecipe:
            calls.append(("pip_install_from_pyproject", path))
            return self

        def env(self, values: dict[str, str]) -> FakeRecipe:
            calls.append(("env", values))
            return self

        def add_local_python_source(self, module: str, *, copy: bool) -> FakeRecipe:
            calls.append(("add_local_python_source", (module, copy)))
            return self

    recipe = FakeRecipe()
    monkeypatch.setattr(
        "modal_computer_use.image._modal",
        lambda: SimpleNamespace(
            Image=SimpleNamespace(debian_slim=lambda python_version: recipe),
        ),
    )

    assert _named_image_recipe(variant="standard", window_manager="xfce") is recipe
    assert ("add_local_python_source", ("modal_computer_use", True)) in calls


def test_named_image_publication_preflight_uses_modal_cli_and_fails_closed(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> object:
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout='[{"tag": "modal-computer-use-standard:abc", "image_id": "im-1"}]',
        )

    monkeypatch.setattr("modal_computer_use.image.subprocess.run", fake_run)

    identities = _published_named_image_identities(environment_name="prod")

    assert identities == {"modal-computer-use-standard:abc"}
    assert calls[0][-2:] == ["--env", "prod"]

    monkeypatch.setattr(
        "modal_computer_use.image.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    with pytest.raises(RuntimeError, match="could not verify existing named Image"):
        _published_named_image_identities(environment_name=None)


def test_modal_billing_report_uses_workspace_or_environment(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Billing:
        def __init__(self, scope: str) -> None:
            self.scope = scope

        def report(self, **kwargs: object) -> list[object]:
            calls.append((self.scope, kwargs))
            return [SimpleNamespace(cost=1)]

    fake_modal = SimpleNamespace(
        Workspace=SimpleNamespace(
            from_context=lambda: SimpleNamespace(billing=Billing("workspace"))
        ),
        Environment=SimpleNamespace(
            from_name=lambda name: SimpleNamespace(billing=Billing(f"environment:{name}"))
        ),
    )
    monkeypatch.setitem(__import__("sys").modules, "modal", fake_modal)

    workspace = modal_billing_report(
        start=SimpleNamespace(),
        end=None,
        resolution="h",
        tag_names=["benchmark"],
    )
    environment = modal_billing_report(
        start=SimpleNamespace(),
        end=None,
        resolution="h",
        tag_names=["benchmark"],
        environment_name="prod",
    )

    assert len(workspace) == 1
    assert len(environment) == 1
    assert [scope for scope, _ in calls] == ["workspace", "environment:prod"]


def test_benchmark_provenance_records_complete_safe_inputs(monkeypatch) -> None:
    monkeypatch.setattr(
        "modal_computer_use.benchmarks.provenance._distribution_version",
        lambda name: {
            "modal-computer-use": "1.0.0",
            "modal": "1.5.2",
            "daytona": None,
            "e2b-desktop": None,
        }[name],
    )

    result = benchmark_provenance(
        caller_path="external-caller",
        modal_region="us-west",
        image_identity=f"modal-computer-use-firefox:{REVISION}",
        cpu=2,
        memory_mib=None,
        gpu=None,
        git_revision=REVISION,
        git_worktree_clean=True,
    )

    assert result["git_revision"] == REVISION
    assert result["git_worktree_clean"] is True
    assert result["caller_path"] == "external-caller"
    assert result["region"] == "us-west"
    assert result["image_identity"].endswith(REVISION)
    assert result["sdk_versions"]["modal"] == "1.5.2"
    assert result["sdk_versions"]["daytona"] is None
    assert result["resolved_resources"]["cpu"]["status"] == "explicit"
    assert result["resolved_resources"]["memory"]["status"] == (
        "provider_default_unavailable"
    )
    assert result["cost_status"] == "see_run_and_surface_cost_status"
