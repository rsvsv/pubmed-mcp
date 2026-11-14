"""Stable review-facing operations; the adapter never imports the MCP entrypoint.

The provider supplies named callbacks. Review orchestration uses this interface;
legacy core names are confined to provider wiring and compatibility tests.
"""
from contextlib import contextmanager
import os
import uuid


OPERATIONS = (
    'write_new_file', 'sha256_file', 'check_datetype', 'check_date', 'expand_date',
    'validate_audit_record', 'audited', 'fetch_records', 'utc_now', 'audit',
    'ris_record', 'audit_report', 'read_pmid_file', 'publish_no_clobber',
    'write_json_atomic', 'implementation_fingerprint', 'export', 'check_recall', 'mask',
)
VALUES = ('recall_limit', 'unknown', 'report_columns', 'source_path', 'error_type')


class ReviewServices:
    def __init__(self, operations, values, get_session, set_session):
        if set(operations) != set(OPERATIONS) or set(values) != set(VALUES):
            raise ValueError('综述服务接口不完整或包含未知操作')
        self._operations = operations
        self._values = values
        self._get_session = get_session
        self._set_session = set_session

    def __getattr__(self, name):
        if name in self._operations:
            return self._operations[name]()
        if name in self._values:
            return self._values[name]()
        raise AttributeError(name)

    @contextmanager
    def audit_session(self, directory):
        """Dedicated CLI process only: preserve and restore its audit configuration."""
        previous_directory = os.environ.get('PUBMED_AUDIT_DIR')
        previous_session = self._get_session()
        try:
            os.environ['PUBMED_AUDIT_DIR'] = str(directory)
            self._set_session(uuid.uuid4().hex)
            yield
        finally:
            self._set_session(previous_session)
            if previous_directory is None:
                os.environ.pop('PUBMED_AUDIT_DIR', None)
            else:
                os.environ['PUBMED_AUDIT_DIR'] = previous_directory
