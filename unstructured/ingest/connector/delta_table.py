import json
import os
import typing as t
from dataclasses import dataclass
from datetime import datetime as dt
from multiprocessing import Process
from pathlib import Path

import pandas as pd
from dataclasses_json.core import Json

from unstructured.ingest.error import SourceConnectionError, SourceConnectionNetworkError
from unstructured.ingest.interfaces import (
    BaseConnectorConfig,
    BaseDestinationConnector,
    BaseSingleIngestDoc,
    BaseSourceConnector,
    IngestDocCleanupMixin,
    SourceConnectorCleanupMixin,
    SourceMetadata,
    WriteConfig,
)
from unstructured.ingest.logger import logger
from unstructured.ingest.utils.table import convert_to_pandas_dataframe
from unstructured.utils import requires_dependencies

if t.TYPE_CHECKING:
    from deltalake import DeltaTable


@dataclass
class SimpleDeltaTableConfig(BaseConnectorConfig):
    table_uri: t.Union[str, Path]
    version: t.Optional[int] = None
    storage_options: t.Optional[t.Dict[str, str]] = None
    without_files: bool = False

    @classmethod
    def from_dict(cls, kvs: Json, **kwargs):
        if (
            isinstance(kvs, dict)
            and "storage_options" in kvs
            and isinstance(kvs["storage_options"], str)
        ):
            kvs["storage_options"] = cls.storage_options_from_str(kvs["storage_options"])
        return super().from_dict(kvs=kvs, **kwargs)

    @staticmethod
    def storage_options_from_str(options_str: str) -> t.Dict[str, str]:
        return {s.split("=")[0].strip(): s.split("=")[1].strip() for s in options_str.split(",")}


@dataclass
class DeltaTableIngestDoc(IngestDocCleanupMixin, BaseSingleIngestDoc):
    connector_config: SimpleDeltaTableConfig
    uri: str
    modified_date: str
    created_at: str
    registry_name: str = "delta-table"

    def uri_filename(self) -> str:
        basename = os.path.basename(self.uri)
        return os.path.splitext(basename)[0]

    @property
    def filename(self):
        return (Path(self.read_config.download_dir) / f"{self.uri_filename()}.csv").resolve()

    @property
    def _output_filename(self):
        """Create filename document id combined with a hash of the query to uniquely identify
        the output file."""
        return Path(self.processor_config.output_dir) / f"{self.uri_filename()}.json"

    def _create_full_tmp_dir_path(self):
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        self._output_filename.parent.mkdir(parents=True, exist_ok=True)

    @requires_dependencies(["fsspec"], extras="delta-table")
    def _get_fs_from_uri(self):
        from fsspec.core import url_to_fs

        try:
            fs, _ = url_to_fs(self.uri)
        except ImportError as error:
            raise ImportError(
                f"uri {self.uri} may be associated with a filesystem that "
                f"requires additional dependencies: {error}",
            )
        return fs

    def update_source_metadata(self, **kwargs):
        fs = kwargs.get("fs", self._get_fs_from_uri())
        version = (
            fs.checksum(self.uri) if fs.protocol != "gs" else fs.info(self.uri).get("etag", "")
        )
        file_exists = fs.exists(self.uri)
        self.source_metadata = SourceMetadata(
            date_created=self.created_at,
            date_modified=self.modified_date,
            version=version,
            source_url=self.uri,
            exists=file_exists,
        )

    @SourceConnectionError.wrap
    @BaseSingleIngestDoc.skip_if_file_exists
    def get_file(self):
        fs = self._get_fs_from_uri()
        self.update_source_metadata(fs=fs)
        logger.info(f"using a {fs} filesystem to collect table data")
        self._create_full_tmp_dir_path()

        df = self._get_df(filesystem=fs)

        logger.info(f"writing {len(df)} rows to {self.filename}")
        df.to_csv(self.filename)

    @SourceConnectionNetworkError.wrap
    def _get_df(self, filesystem) -> pd.DataFrame:
        import pyarrow.parquet as pq

        return pq.ParquetDataset(self.uri, filesystem=filesystem).read_pandas().to_pandas()


@dataclass
class DeltaTableSourceConnector(SourceConnectorCleanupMixin, BaseSourceConnector):
    connector_config: SimpleDeltaTableConfig
    delta_table: t.Optional["DeltaTable"] = None

    def check_connection(self):
        pass

    @requires_dependencies(["deltalake"], extras="delta-table")
    def initialize(self):
        from deltalake import DeltaTable

        self.delta_table = DeltaTable(
            table_uri=self.connector_config.table_uri,
            version=self.connector_config.version,
            storage_options=self.connector_config.storage_options,
            without_files=self.connector_config.without_files,
        )
        rows = self.delta_table.to_pyarrow_dataset().count_rows()
        if not rows > 0:
            raise ValueError(f"no data found at {self.connector_config.table_uri}")
        logger.info(f"processing {rows} rows of data")

    def get_ingest_docs(self):
        """Batches the results into distinct docs"""
        if not self.delta_table:
            raise ValueError("delta table was never initialized")
        actions = self.delta_table.get_add_actions().to_pandas()
        mod_date_dict = {
            row["path"]: str(row["modification_time"]) for _, row in actions.iterrows()
        }
        created_at = dt.fromtimestamp(self.delta_table.metadata().created_time / 1000)
        return [
            DeltaTableIngestDoc(
                connector_config=self.connector_config,
                processor_config=self.processor_config,
                read_config=self.read_config,
                uri=uri,
                modified_date=mod_date_dict[os.path.basename(uri)],
                created_at=str(created_at),
            )
            for uri in self.delta_table.file_uris()
        ]


@dataclass
class DeltaTableWriteConfig(WriteConfig):
    drop_empty_cols: bool = False
    overwrite_schema: bool = False
    mode: t.Literal["error", "append", "overwrite", "ignore"] = "error"


@dataclass
class DeltaTableDestinationConnector(BaseDestinationConnector):
    write_config: DeltaTableWriteConfig
    connector_config: SimpleDeltaTableConfig

    @requires_dependencies(["deltalake"], extras="delta-table")
    def initialize(self):
        pass

    def check_connection(self):
        pass

    def write_dict(self, *args, elements_dict: t.List[t.Dict[str, t.Any]], **kwargs) -> None:
        from deltalake.writer import write_deltalake

        df = convert_to_pandas_dataframe(
            elements_dict=elements_dict,
            drop_empty_cols=self.write_config.drop_empty_cols,
        )
        logger.info(
            f"writing {len(df)} rows to destination table "
            f"at {self.connector_config.table_uri}\ndtypes: {df.dtypes}",
        )
        # NOTE: deltalake writer on Linux sometimes can finish but still trigger a SIGABRT and cause
        # ingest to fail, even though all tasks are completed normally. Putting the writer into a
        # process mitigates this issue by ensuring python interpreter waits properly for deltalake's
        # rust backend to finish
        writer = Process(
            target=write_deltalake,
            kwargs={
                "table_or_uri": self.connector_config.table_uri,
                "data": df,
                "mode": self.write_config.mode,
                "overwrite_schema": self.write_config.overwrite_schema,
            },
        )
        writer.start()
        writer.join()

    @requires_dependencies(["deltalake"], extras="delta-table")
    def write(self, docs: t.List[BaseSingleIngestDoc]) -> None:
        elements_dict: t.List[t.Dict[str, t.Any]] = []
        for doc in docs:
            local_path = doc._output_filename
            with open(local_path) as json_file:
                element_dict = json.load(json_file)
                logger.info(f"converting {len(element_dict)} rows from content in {local_path}")
                elements_dict.extend(element_dict)
        self.write_dict(elements_dict=elements_dict)
