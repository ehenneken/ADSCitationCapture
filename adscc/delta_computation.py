import os
from datetime import datetime
import postgres_copy
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy import create_engine
from adscc.models import RawCitation, CitationChanges
from adsputils import setup_logging
import adsmsg

class DeltaComputation():
    """
    Loads refids file into DB, crossmatches with the previous loaded file (if
    exists) and identifies citation changes. The class is iterable.
    """

    def __init__(self, sqlachemy_url, group_changes_in_chunks_of=1, sqlalchemy_echo=False, schema_prefix="citation_capture_", force=False):
        """
        Initializes the class and prepares DB connection.

        :param sqlachemy_url: URL to connect to the DB.
        :param group_changes_in_chunks_of: Number of citation changes to be
            grouped when iterating.
        :param sqlalchemy_echo: Print every SQL statement.
        :param schema_prefix: Data is stored in schemas that correspond to a
            prefix + file last access date.
        :param force: If tables already exists in DB, drop them and re-ingest.
        """
        self.engine = create_engine(sqlachemy_url, echo=sqlalchemy_echo)
        self.connection = self.engine.connect()
        self.session = sessionmaker(bind=self.engine)()
        #
        self.logger = setup_logging(__name__)
        #
        self.table_name = RawCitation.__tablename__
        self.expanded_table_name = "expanded_" + self.table_name
        self.joint_table_name = CitationChanges.__tablename__
        self.schema_prefix = schema_prefix
        self.schema_name = None
        self.previous_schema_name = None
        self.input_refids_filename = None
        self.group_changes_in_chunks_of=group_changes_in_chunks_of
        self.offset = 0
        self.n_changes = 0
        self.force = force

    def compute(self, input_refids_filename):
        """
        Loads refids file into DB, crossmatches with the previous loaded file
        (if exists) and identifies citation changes.

        :param input_refids_filename: Path to the file to be imported.
        """
        self.offset = 0
        self.input_refids_filename = input_refids_filename
        self._setup_schemas()
        if self.force or self.joint_table_name not in Inspector.from_engine(self.engine).get_table_names(schema=self.schema_name):
            self.logger.info("Importing '%s' into table '%s.%s'", self.input_refids_filename, self.schema_name, self.table_name)
            try:
                self._import()
            except:
                self.logger.exception("Problem importing file '%s', dropping schema '%s'", self.input_refids_filename, self.schema_name)
                # Roll back created schema for this file
                drop_schema = "drop schema {0} cascade;"
                self._execute_sql(drop_schema, self.schema_name)
                raise
            if self.previous_schema_name is not None:
                self.logger.info("Comparing table '%s.%s' with previous table '%s.%s'", self.schema_name, self.table_name, self.previous_schema_name, self.table_name)
            self._join_tables()
            self._calculate_delta()
            self.logger.info("Created table '%s.%s'", self.schema_name, self.joint_table_name)
        else:
            self.logger.info("Table '%s.%s' already exists, re-using results without importing the specified file '%s'", self.schema_name, self.joint_table_name, self.input_refids_filename)
        self.n_changes = self._compute_n_changes()
        self.logger.info("Table '%s.%s' contains '%s' citation changes", self.schema_name, self.joint_table_name, self.n_changes)

    def _execute_sql(self, sql_template, *args):
        """Build sql from template and execute"""
        sql_command = sql_template.format(*args)
        self.logger.debug("Executing SQL: %s", sql_command)
        return self.connection.execute(sql_command)

    def _citation_changes_query(self):
        if self.joint_table_name in Inspector.from_engine(self.engine).get_table_names(schema=self.schema_name):
            CitationChanges.__table__.schema = self.schema_name
        ## Only consider Zenodo and ASCL records
        #sqlalchemy_query = self.session.query(CitationChanges).filter((CitationChanges.new_content.like('%zenodo%')) | (CitationChanges.new_pid.is_(True)))
        ## Only consider Zenodo
        #sqlalchemy_query = self.session.query(CitationChanges).filter(CitationChanges.new_content.like('%zenodo%'))
        # Consider Zenodo, ASCL and URL records (all of them)
        sqlalchemy_query = self.session.query(CitationChanges)
        return sqlalchemy_query

    def __iter__(self):
        return self

    def next(self): # Python 3: def __next__(self)
        """Iterates over the results, grouping changes in chunks"""
        if self.offset >= self.n_changes or self.n_changes == 0:
            raise StopIteration
        else:
            citation_changes = adsmsg.CitationChanges()
            # Get citation changes from DB
            for instance in self._citation_changes_query().offset(self.offset).limit(self.group_changes_in_chunks_of).yield_per(100):
                ## Build protobuf message
                citation_change = citation_changes.changes.add()
                # Use new_ or previous_ fields depending if status is NEW/UPDATED or DELETED
                prefix = "previous_" if instance.status == "DELETED" else "new_"
                citation_change.citing = getattr(instance, prefix+"citing")
                resolved = getattr(instance, prefix+"resolved")
                if resolved:
                    # Only accept cited bibcode if score is 1 (resolved)
                    citation_change.cited = getattr(instance, prefix+"cited")
                else:
                    citation_change.cited = '...................'
                if getattr(instance, prefix+"doi"):
                    citation_change.content_type = adsmsg.CitationChangeContentType.doi
                elif getattr(instance, prefix+"pid"):
                    citation_change.content_type = adsmsg.CitationChangeContentType.pid
                elif getattr(instance, prefix+"url"):
                    citation_change.content_type = adsmsg.CitationChangeContentType.url
                citation_change.content = getattr(instance, prefix+"content")
                citation_change.resolved = getattr(instance, prefix+"resolved")
                citation_change.status = getattr(adsmsg.Status, instance.status.lower())
            self.session.commit()

            self.offset += self.group_changes_in_chunks_of
            return citation_changes

    def _setup_schemas(self):
        """Create new schema, identify previous and drop older ones"""
        # Schema name for current file
        last_modification_date = datetime.fromtimestamp(os.stat(self.input_refids_filename).st_mtime)
        self.schema_name = self.schema_prefix + last_modification_date.strftime("%Y%m%d_%H%M%S")

        # Create schema if needed
        existing_schema_names = Inspector.from_engine(self.engine).get_schema_names()
        existing_schema_names = filter(lambda x: x.startswith(self.schema_prefix), existing_schema_names)
        if self.schema_name not in existing_schema_names:
            self.connection.execute(CreateSchema(self.schema_name))
            filtered_existing_schema_names = existing_schema_names
        else:
            filtered_existing_schema_names = filter(lambda x: x != self.schema_name, existing_schema_names)

        # Determine previous schema name if any
        if len(filtered_existing_schema_names) > 0:
            filtered_existing_schema_names.sort(reverse=True)
            self.previous_schema_name = filtered_existing_schema_names[0]

            # Verify the data that is going to be imported is newer than the data already imported
            schema_date_fingerprint = int(self.schema_name.replace(self.schema_prefix, "").replace("_", ""))
            previous_schema_date_fingerprint = int(self.previous_schema_name.replace(self.schema_prefix, "").replace("_", ""))
            if previous_schema_date_fingerprint >= schema_date_fingerprint:
                raise Exception("The data to be imported has a date fingerprint '{0}' older than the data already in the DB '{1}'".format(self.schema_name, self.previous_schema_name))

            # Drop old schemas (just keep last 3)
            if len(filtered_existing_schema_names) > 2:
                for old_schema_name in filtered_existing_schema_names[2:]:
                    drop_schema = "drop schema {0} cascade;"
                    self._execute_sql(drop_schema, old_schema_name)

    def _import(self):
        """Import from file, expand its JSON column and delete duplicates"""
        self._copy_from_file()
        self._expand_json()
        self._delete_dups()

        try:
            self._verify_input_data()
        except:
            self.logger.exception("Input data does not comply with some assumptions")
            raise

    def _copy_from_file(self):
        """Import file into DB"""
        table_already_exists = self.table_name in Inspector.from_engine(self.engine).get_table_names(schema=self.schema_name)
        if table_already_exists and self.force:
            self.logger.info("Dropping table '%s.%s' due to force mode", self.schema_name, self.table_name)
            drop_table = "drop table if exists {0}.{1};"
            self._execute_sql(drop_table, self.schema_name, self.table_name)
        elif table_already_exists:
            return

        RawCitation.__table__.schema = self.schema_name
        RawCitation.__table__.create(bind=self.engine)

        # Import a tab-delimited file
        with open(self.input_refids_filename) as fp:
            l = postgres_copy.copy_from(fp, RawCitation, self.engine, columns=('bibcode', 'payload'))


    def _expand_json(self):
        """Extract data from the JSON column as individual columns"""
        table_already_exists = self.expanded_table_name in Inspector.from_engine(self.engine).get_table_names(schema=self.schema_name)
        if table_already_exists and self.force:
            self.logger.info("Dropping table '%s.%s' due to force mode", self.schema_name, self.expanded_table_name)
            drop_table = "drop table if exists {0}.{1};"
            self._execute_sql(drop_table, self.schema_name, self.expanded_table_name)
        elif table_already_exists:
            return

        # Expand ignoring the source field, keeping only information about
        # score == "1" which have resolved bibcodes in the cited field
        # and ordered by citing, data and reverse resolved to guarantee that
        # duplicates where there is one entry that is resolved and others that
        # don't, the one that is resolved is the one kept (the rest are removed,
        # see _delete_dups where MIN(id) is kept)
        create_expanded_table = \
                "create table {0}.{2} as \
                    select id, \
                        payload->>'citing' as citing, \
                        payload->>'cited' as cited, \
                        (payload->>'doi' is not null) as doi, \
                        (payload->>'pid' is not null) as pid, \
                        (payload->>'url' is not null) as url, \
                        concat(payload->>'doi'::text, payload->>'pid'::text, payload->>'url'::text) as content, \
                        (payload->>'score' is not null and payload->>'score' = '1') as resolved \
                    from {0}.{1} order by citing asc, content asc, resolved desc;"
        self._execute_sql(create_expanded_table, self.schema_name, self.table_name, self.expanded_table_name)


    def _delete_dups(self):
        """
        The input file can have duplicates such as:

           2011arXiv1112.0312C	{"cited":"2012ascl.soft03003C","citing":"2011arXiv1112.0312C","pid":"ascl:1203.003","score":"1","source":"/proj/ads/references/resolved/arXiv/1112/0312.raw.result:10"}
           2011arXiv1112.0312C	{"cited":"2012ascl.soft03003C","citing":"2011arXiv1112.0312C","pid":"ascl:1203.003","score":"1","source":"/proj/ads/references/resolved/AUTHOR/2012/0605.pairs.result:89"}

        Because the same citation was identified in more than one source.
        We can safely ignore them but in case there is any of these dups
        that were not resolved, the resolved one should be prioriticed.
        """
        delete_duplicates_sql = \
            "DELETE FROM {0}.{1} WHERE id IN ( \
                SELECT id FROM \
                    (SELECT id, row_number() over(partition by citing, content order by resolved desc) AS dup_id FROM {0}.{1}) t \
                WHERE t.dup_id > 1 \
            )"
        self._execute_sql(delete_duplicates_sql, self.schema_name, self.expanded_table_name)

    def _compute_n_changes(self):
        """Count how many citation changes were identified"""
        if self.joint_table_name in Inspector.from_engine(self.engine).get_table_names(schema=self.schema_name):
            n_changes = self._citation_changes_query().count()
            return n_changes
        else:
            return 0

    def _verify_input_data(self):
        """
        Delta computations assume input data follow certain logic and here it
        is checked to be true:

        - At least one field contains a value for doi, pid or url
        - Only one field contains a value for doi, pid or url
        - No duplicates
        """
        ## Check assumptions
        # - At least one field contains a value for doi, pid or url
        count_all_fields_null_sql = \
                "select count(*) \
                    from {0}.{1} \
                    where (\
                            not doi \
                            and not pid \
                            and not url \
                        );"
        n_all_fields_null = self._execute_sql(count_all_fields_null_sql, self.schema_name, self.expanded_table_name).scalar()
        if n_all_fields_null > 0:
            raise Exception("There is at least an entry with all doi, pid and url fields set to null")

        # - Only one field contains a value for doi, pid or url
        count_too_many_fields_not_null_sql = \
                "select count(*) \
                    from {0}.{1} \
                    where (\
                            (doi and pid and not url) \
                            or (doi and not pid and url) \
                            or (not doi and pid and url) \
                            or (doi and pid and url) \
                        );"
        n_too_many_fields_not_null = self._execute_sql(count_too_many_fields_not_null_sql, self.schema_name, self.expanded_table_name).scalar()
        if n_too_many_fields_not_null > 0:
            raise Exception("There is at least an entry with two or more doi, pid and url fields set to a value")

        # - No duplicates
        count_duplicates_sql = \
                "select count(*) \
                    from {0}.{1} \
                    group by citing, content \
                    having count(*) > 1;"
        n_duplicates = self._execute_sql(count_duplicates_sql, self.schema_name, self.expanded_table_name).scalar()
        if n_duplicates > 0:
            raise Exception("There are duplicate entries with the same citing, doi, pid and url fields")

    def _join_tables(self):
        """
        Full join between the previous and the new table but keeping only NEW,
        DELETED and UPDATED records. Previous and new values are preserved in
        columns with names composed by a prefix "previous_" or "new_".

        If there was no previous table, a new fake joint table is built with
        null values for all the "previous_" columns.
        """
        # ~1h
        table_already_exists = self.joint_table_name in Inspector.from_engine(self.engine).get_table_names(schema=self.schema_name)
        if table_already_exists and self.force:
            self.logger.info("Dropping table '%s.%s' due to force mode", self.schema_name, self.joint_table_name)
            drop_table = "drop table if exists {0}.{1};"
            self._execute_sql(drop_table, self.schema_name, self.joint_table_name)
        elif table_already_exists:
            return

        if self.previous_schema_name is None:
            # Not really a JOIN since there is no previous table
            joint_table_sql = \
                    "create table {0}.{2} as \
                        select \
                            {0}.{1}.id as new_id, \
                            {0}.{1}.citing as new_citing, \
                            {0}.{1}.cited as new_cited, \
                            {0}.{1}.doi as new_doi, \
                            {0}.{1}.pid as new_pid, \
                            {0}.{1}.url as new_url, \
                            {0}.{1}.content as new_content, \
                            {0}.{1}.resolved as new_resolved, \
                            cast(null as text) as previous_id, \
                            cast(null as text) as previous_citing, \
                            cast(null as text) as previous_cited, \
                            cast(null as boolean) as previous_doi, \
                            cast(null as boolean) as previous_pid, \
                            cast(null as boolean) as previous_url, \
                            cast(null as text) as previous_content, \
                            cast(null as boolean) as previous_resolved \
                        from {0}.{1};"
            self._execute_sql(joint_table_sql, self.schema_name, self.expanded_table_name, self.joint_table_name)
        else:
            joint_table_sql = \
                    "create table {0}.{3} as \
                        select \
                            {0}.{2}.id as new_id, \
                            {0}.{2}.citing as new_citing, \
                            {0}.{2}.cited as new_cited, \
                            {0}.{2}.doi as new_doi, \
                            {0}.{2}.pid as new_pid, \
                            {0}.{2}.url as new_url, \
                            {0}.{2}.content as new_content, \
                            {0}.{2}.resolved as new_resolved, \
                            {1}.{2}.id as previous_id, \
                            {1}.{2}.citing as previous_citing, \
                            {1}.{2}.cited as previous_cited, \
                            {1}.{2}.doi as previous_doi, \
                            {1}.{2}.pid as previous_pid, \
                            {1}.{2}.url as previous_url, \
                            {1}.{2}.content as previous_content, \
                            {1}.{2}.resolved as previous_resolved \
                        from {1}.{2} full join {0}.{2} \
                        on \
                            {0}.{2}.citing={1}.{2}.citing \
                            and {0}.{2}.content={1}.{2}.content \
                        where \
                            ({0}.{2}.id is not null and {1}.{2}.id is null) \
                            or ({0}.{2}.id is null and {1}.{2}.id is not null) \
                            or ({0}.{2}.id is not null and {1}.{2}.id is not null and ({0}.{2}.cited<>{1}.{2}.cited or {0}.{2}.resolved<>{1}.{2}.resolved)) \
                        ;"
            self._execute_sql(joint_table_sql, self.schema_name, self.previous_schema_name, self.expanded_table_name, self.joint_table_name)

        add_id_column_sql = "ALTER TABLE {0}.{1} ADD COLUMN id SERIAL PRIMARY KEY;"
        self._execute_sql(add_id_column_sql, self.schema_name, self.joint_table_name)

        status_enum_name = "status_type"
        enum_names = [e['name'] for e in Inspector.from_engine(self.engine).get_enums(schema=self.schema_name)]
        if status_enum_name not in enum_names:
            create_enum_type = "CREATE TYPE {0}.{1} AS ENUM ('NEW', 'DELETED', 'UPDATED');"
            self._execute_sql(create_enum_type, self.schema_name, status_enum_name)
        add_status_column_sql = "ALTER TABLE {0}.{1} ADD COLUMN status {0}.{2};"
        self._execute_sql(add_status_column_sql, self.schema_name, self.joint_table_name, status_enum_name)

        ## ~1h
        #create_index = "CREATE INDEX status_idx ON {0}.{1} (status);"
        #self._execute_sql(create_index, self.schema_name,  self.joint_table_name)


    def _calculate_delta(self):
        """Classify citation changes as NEW, UPDATED or DELETED"""
        update_status_updated_sql = \
           "update {0}.{1} \
            set status='UPDATED' \
            where \
                {0}.{1}.status is null \
                and {0}.{1}.new_id is not null \
                and {0}.{1}.previous_id is not null \
                and ({0}.{1}.new_cited<>{0}.{1}.previous_cited \
                    or {0}.{1}.new_resolved<>{0}.{1}.previous_resolved);"
        self._execute_sql(update_status_updated_sql, self.schema_name, self.joint_table_name)

        update_status_new_sql = \
           "update {0}.{1} \
            set status='NEW' \
            where \
                {0}.{1}.status is null \
                and {0}.{1}.new_id is not null \
                and {0}.{1}.previous_id is null;"

        self._execute_sql(update_status_new_sql, self.schema_name, self.joint_table_name)

        update_status_deleted_sql = \
           "update {0}.{1} \
            set status='DELETED' \
            where \
                {0}.{1}.status is null \
                and {0}.{1}.new_id is null \
                and {0}.{1}.previous_id is not null;"
        self._execute_sql(update_status_deleted_sql, self.schema_name, self.joint_table_name)


