from deployment.tui import paths


def test_scripts_dir_contains_install_ecs():
    assert (paths.SCRIPTS_DIR / "install-ecs.sh").is_file()


def test_installer_exists():
    assert paths.installer_py().is_file()


def test_build_lambdas_script_exists():
    assert paths.BUILD_LAMBDAS_SH.is_file()
