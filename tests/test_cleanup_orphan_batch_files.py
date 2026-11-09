import cleanup_orphan_batch_files as cleanup


class TestValidate:
    def test_accepts_a_real_matching_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cleanup, "_TMP_DIR", tmp_path)
        target = tmp_path / "batch_abc.ipc"
        target.write_text("data")

        assert cleanup._validate(str(target)) == target

    def test_rejects_a_symlink(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cleanup, "_TMP_DIR", tmp_path)
        real = tmp_path / "somewhere_else.txt"
        real.write_text("data")
        link = tmp_path / "batch_link.ipc"
        link.symlink_to(real)

        assert cleanup._validate(str(link)) is None
        assert real.exists()

    def test_rejects_a_path_outside_tmp_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cleanup, "_TMP_DIR", tmp_path)
        nested = tmp_path / "nested"
        nested.mkdir()
        target = nested / "batch_abc.ipc"
        target.write_text("data")

        assert cleanup._validate(str(target)) is None

    def test_rejects_a_name_not_matching_the_pattern(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cleanup, "_TMP_DIR", tmp_path)
        target = tmp_path / "not_a_batch_file.txt"
        target.write_text("data")

        assert cleanup._validate(str(target)) is None

    def test_rejects_a_missing_file(self, tmp_path):
        assert cleanup._validate(str(tmp_path / "batch_missing.ipc")) is None


class TestMain:
    def test_deletes_valid_files_and_reports_success(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cleanup, "_TMP_DIR", tmp_path)
        target = tmp_path / "batch_abc.ipc"
        target.write_text("data")

        exit_code = cleanup.main([str(target)])

        assert exit_code == 0
        assert not target.exists()

    def test_skips_invalid_paths_and_reports_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cleanup, "_TMP_DIR", tmp_path)
        valid = tmp_path / "batch_abc.ipc"
        valid.write_text("data")
        invalid = tmp_path / "unrelated.txt"
        invalid.write_text("data")

        exit_code = cleanup.main([str(valid), str(invalid)])

        assert exit_code == 1
        assert not valid.exists()
        assert invalid.exists()

    def test_no_arguments_returns_error(self):
        assert cleanup.main([]) == 1
