from __future__ import annotations

from launch_website import load_env_values


def test_launcher_loads_allowlisted_private_runtime_values_without_arbitrary_keys(tmp_path) -> None:
    (tmp_path / ".env.local").write_text(
        "\n".join(
            (
                "LUX3D_API_KEY" + "=fixture-secret-never-printed",
                "LUX3D_BASE_URL='https://provider.example.test'",
                "WEBSITE_VISION_API_KEY=fixture-vision-secret",
                "WEBSITE_VISION_BASE_URL=https://vision.example.test/v1",
                "WEBSITE_VISION_MODEL=vision-model",
                "BLENDER_WORKER_ENABLED=true",
                "BLENDER_EXECUTABLE=blender.exe",
                "OUTPUT_ROOT=./private-output",
                "UNSAFE_SHELL_SWITCH=should-not-pass",
                "EMPTY_VALUE=",
            )
        ),
        encoding="utf-8",
    )

    values = load_env_values(tmp_path)

    assert values["LUX3D_API_KEY"] == "fixture-secret-never-printed"
    assert values["LUX3D_BASE_URL"] == "https://provider.example.test"
    assert values["WEBSITE_VISION_API_KEY"] == "fixture-vision-secret"
    assert values["WEBSITE_VISION_BASE_URL"] == "https://vision.example.test/v1"
    assert values["WEBSITE_VISION_MODEL"] == "vision-model"
    assert values["BLENDER_WORKER_ENABLED"] == "true"
    assert values["BLENDER_EXECUTABLE"] == "blender.exe"
    assert values["OUTPUT_ROOT"] == "./private-output"
    assert "UNSAFE_SHELL_SWITCH" not in values
    assert "EMPTY_VALUE" not in values
