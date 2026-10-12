"""html_only_global_pipeline

Revision ID: c9e7a5b41201
Revises: 8dcd4fd9b429
Create Date: 2026-10-12 20:39:05.338729
"""

from alembic import op
import sqlalchemy as sa
import greek_news.db


revision = 'c9e7a5b41201'
down_revision = '8dcd4fd9b429'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('worker_heartbeats',
    sa.Column('id', sa.String(length=100), nullable=False),
    sa.Column('updated_at', greek_news.db.UTCDateTime(timezone=True), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('article_facets',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('article_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('value', sa.String(length=200), nullable=False),
    sa.ForeignKeyConstraint(['article_id'], ['articles.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('article_id', 'kind', 'value')
    )
    with op.batch_alter_table('article_facets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_article_facets_article_id'), ['article_id'], unique=False)
        batch_op.create_index('ix_article_facets_lookup', ['kind', 'value', 'article_id'], unique=False)

    op.create_table('crawl_urls',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('source_id', sa.String(length=36), nullable=False),
    sa.Column('url', sa.String(length=2000), nullable=False),
    sa.Column('title_hint', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('depth', sa.Integer(), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('discovered_at', greek_news.db.UTCDateTime(timezone=True), nullable=False),
    sa.Column('next_fetch_at', greek_news.db.UTCDateTime(timezone=True), nullable=False),
    sa.Column('fetched_at', greek_news.db.UTCDateTime(timezone=True), nullable=True),
    sa.Column('etag', sa.String(length=500), nullable=True),
    sa.Column('last_modified', sa.String(length=500), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('source_item_id', sa.String(length=36), nullable=True),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
    sa.ForeignKeyConstraint(['source_item_id'], ['source_items.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_id', 'url')
    )
    with op.batch_alter_table('crawl_urls', schema=None) as batch_op:
        batch_op.create_index('ix_crawl_urls_due', ['state', 'next_fetch_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_crawl_urls_source_id'), ['source_id'], unique=False)

    op.create_table('place_aliases',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('place_id', sa.String(length=160), nullable=False),
    sa.Column('normalized_name', sa.String(length=200), nullable=False),
    sa.ForeignKeyConstraint(['place_id'], ['places.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('place_id', 'normalized_name')
    )
    with op.batch_alter_table('place_aliases', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_place_aliases_normalized_name'), ['normalized_name'], unique=False)
        batch_op.create_index(batch_op.f('ix_place_aliases_place_id'), ['place_id'], unique=False)

    with op.batch_alter_table('llm_calls', naming_convention={'uq': 'uq_%(table_name)s_%(column_0_name)s'}) as batch_op:
        batch_op.add_column(sa.Column('stage', sa.String(length=30), nullable=False, server_default='draft'))
        batch_op.add_column(sa.Column('attempt', sa.Integer(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('response', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('request_hash', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_llm_calls_job_id'), ['job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_llm_calls_request_hash'), ['request_hash'], unique=False)
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints('llm_calls'):
            if constraint['column_names'] == ['job_id']:
                batch_op.drop_constraint(constraint['name'] or 'uq_llm_calls_job_id', type_='unique')
        batch_op.create_unique_constraint('uq_llm_calls_stage_attempt', ['job_id', 'stage', 'attempt'])

    with op.batch_alter_table('source_revisions', naming_convention={'uq': 'uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s'}) as batch_op:
        batch_op.add_column(sa.Column('analysis', sa.JSON(), nullable=False, server_default='{}'))
        batch_op.add_column(sa.Column('analysis_state', sa.String(length=20), nullable=False, server_default='pending'))
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints('source_revisions'):
            if set(constraint['column_names']) == {'source_item_id', 'content_hash'}:
                batch_op.drop_constraint(constraint['name'] or 'uq_source_revisions_source_item_id_content_hash', type_='unique')
        batch_op.create_index(batch_op.f('ix_source_revisions_analysis_state'), ['analysis_state'], unique=False)

    revisions = sa.table('source_revisions', sa.column('id', sa.String()), sa.column('evidence_claims', sa.JSON()), sa.column('analysis_state', sa.String()))
    for row in op.get_bind().execute(sa.select(revisions.c.id, revisions.c.evidence_claims)):
        if row.evidence_claims:
            op.get_bind().execute(revisions.update().where(revisions.c.id == row.id).values(analysis_state='completed'))

    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('country_code', sa.String(length=2), nullable=False, server_default='GR'))
        batch_op.add_column(sa.Column('locality_hint', sa.String(length=200), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('entry_urls', sa.JSON(), nullable=False, server_default='[]'))
        batch_op.add_column(sa.Column('article_url_pattern', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('index_url_pattern', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('link_selector', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('is_directory', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('next_request_at', greek_news.db.UTCDateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('robots_txt', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('robots_checked_at', greek_news.db.UTCDateTime(timezone=True), nullable=True))



def downgrade():
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.drop_column('robots_checked_at')
        batch_op.drop_column('robots_txt')
        batch_op.drop_column('next_request_at')
        batch_op.drop_column('is_directory')
        batch_op.drop_column('link_selector')
        batch_op.drop_column('index_url_pattern')
        batch_op.drop_column('article_url_pattern')
        batch_op.drop_column('entry_urls')
        batch_op.drop_column('locality_hint')
        batch_op.drop_column('country_code')

    with op.batch_alter_table('source_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_source_revisions_analysis_state'))
        batch_op.drop_column('analysis_state')
        batch_op.drop_column('analysis')
        batch_op.create_unique_constraint('source_revisions_source_item_id_content_hash_key', ['source_item_id', 'content_hash'])

    with op.batch_alter_table('llm_calls', schema=None) as batch_op:
        batch_op.drop_constraint('uq_llm_calls_stage_attempt', type_='unique')
        batch_op.create_unique_constraint('llm_calls_job_id_key', ['job_id'])
        batch_op.drop_index(batch_op.f('ix_llm_calls_request_hash'))
        batch_op.drop_index(batch_op.f('ix_llm_calls_job_id'))
        batch_op.drop_column('request_hash')
        batch_op.drop_column('response')
        batch_op.drop_column('attempt')
        batch_op.drop_column('stage')

    with op.batch_alter_table('place_aliases', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_place_aliases_place_id'))
        batch_op.drop_index(batch_op.f('ix_place_aliases_normalized_name'))

    op.drop_table('place_aliases')
    with op.batch_alter_table('crawl_urls', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_crawl_urls_source_id'))
        batch_op.drop_index('ix_crawl_urls_due')

    op.drop_table('crawl_urls')
    with op.batch_alter_table('article_facets', schema=None) as batch_op:
        batch_op.drop_index('ix_article_facets_lookup')
        batch_op.drop_index(batch_op.f('ix_article_facets_article_id'))

    op.drop_table('article_facets')
    op.drop_table('worker_heartbeats')
    # ### end Alembic commands ###