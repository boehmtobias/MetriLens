from metrilens.db import get_engine, get_session
from metrilens.models import Scenario


class TestGetEngine:
    def test_creates_db_file_and_parent_dir(self, tmp_path):
        db_path = tmp_path / "nested" / "metrilens.db"
        get_engine(db_path)
        assert db_path.exists()

    def test_creates_all_tables(self, tmp_path):
        engine = get_engine(tmp_path / "metrilens.db")
        with get_session(engine) as session:
            session.add(Scenario(name="s", steps=["a"]))
            session.commit()
            assert session.query(Scenario).count() == 1

    def test_reopening_reuses_existing_data(self, tmp_path):
        db_path = tmp_path / "metrilens.db"
        with get_session(get_engine(db_path)) as session:
            session.add(Scenario(name="s", steps=["a"]))
            session.commit()

        with get_session(get_engine(db_path)) as session:
            assert session.query(Scenario).count() == 1
