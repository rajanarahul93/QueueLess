import uuid
from datetime import datetime
from typing import Annotated, Any

from sqlalchemy import DateTime, MetaData, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Deterministic names so migrations and models agree. Migrations pass names explicitly; ck names
# embed the table, so they must be wrapped in op.f() there to avoid a double prefix.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        str: Text(),
        datetime: DateTime(timezone=True),
        # Any: jsonb holds free-form documents (open_hours, settings, event payloads).
        dict[str, Any]: JSONB(),
    }


UuidPk = Annotated[
    uuid.UUID, mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
]
NowTs = Annotated[datetime, mapped_column(server_default=func.now())]
