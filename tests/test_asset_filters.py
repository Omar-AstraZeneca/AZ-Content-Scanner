from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ScanRun
from app.routers.assets import _latest_run_ids


def test_latest_run_is_selected_per_site_despite_missing_optional_metadata():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        old_run = ScanRun(
            market="CAMCAR",
            site_url="https://www.azmed.cr",
            site_country="Costa Rica",
            site_category="Portal HCP",
            status="DONE",
        )
        new_run = ScanRun(
            market="CAMCAR",
            site_url="https://www.azmed.cr",
            status="DONE",
        )
        db.add_all([old_run, new_run])
        db.commit()

        assert _latest_run_ids(db) == [new_run.id]