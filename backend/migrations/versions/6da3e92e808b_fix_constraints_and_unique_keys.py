"""fix_constraints_and_unique_keys

Revision ID: 6da3e92e808b
Revises: 8ec11a64fc03
Create Date: 2026-03-07 14:30:42.544645

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6da3e92e808b'
down_revision: Union[str, Sequence[str], None] = '8ec11a64fc03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
