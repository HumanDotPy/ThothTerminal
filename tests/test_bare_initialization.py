"""Fresh defaults and repeated initialization preserve the user's workspace."""
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    Base, Company, Document, Note, ResearchReport, ResearchUniverse,
    Source, SourceActor, SourceState, SupplyChain, Thesis,
)
from thoth_terminal.seed import seed_all


def test_initialization_is_bare_and_preserves_existing_research():
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        seed_all(engine)
        with Session(engine) as session:
            for model in (Company, Document, Note, ResearchReport, ResearchUniverse, SourceActor, SupplyChain, Thesis):
                assert session.query(model).count() == 0, model.__name__
            assert {source.name for source in session.query(Source)} == {"internal", "local_deep_research_library"}
            assert all(state.updated_at is not None for state in session.query(SourceState))
            source = session.query(Source).filter_by(name="internal").one()
            source.config = {"user_setting": "keep"}
            source.priority = 42
            session.add(Company(slug="synthetic-user-company", name="Synthetic user company", created_at=utcnow()))
            session.commit()
        seed_all(engine)
        with Session(engine) as session:
            assert session.query(Company).one().slug == "synthetic-user-company"
            source = session.query(Source).filter_by(name="internal").one()
            assert source.config == {"user_setting": "keep"}
            assert source.priority == 42
            assert session.query(Source).count() == 2
            assert session.query(SourceState).count() == 2
    finally:
        engine.dispose()
