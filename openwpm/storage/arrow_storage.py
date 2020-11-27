import asyncio
import logging
import random
from abc import abstractmethod
from collections import defaultdict
from typing import Any, DefaultDict, Dict, List

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow import Table

from openwpm.types import VisitId

from .parquet_schema import PQ_SCHEMAS
from .storage_providers import StructuredStorageProvider, TableName

INCOMPLETE_VISITS = TableName("incomplete_visits")
CACHE_SIZE = 500


class ArrowProvider(StructuredStorageProvider):
    """This class implements a StructuredStorage provider that
    serializes records into the arrow format
    """

    def __init__(self) -> None:
        super().__init__()
        self.logger = logging.getLogger("openwpm")

        def factory_function() -> DefaultDict[TableName, List[Dict[str, Any]]]:
            return defaultdict(list)

        # Raw records per VisitId and Table
        self._records: DefaultDict[
            VisitId, DefaultDict[TableName, List[Dict[str, Any]]]
        ] = defaultdict(factory_function)

        # Record batches by TableName
        self._batches: DefaultDict[TableName, List[pa.RecordBatch]] = defaultdict(list)

        # Used to synchronize the finalizing and the flushing
        self.storing_condition = asyncio.Condition()
        self.flushing = False

        self._instance_id = random.getrandbits(32)

    async def store_record(
        self, table: TableName, visit_id: VisitId, record: Dict[str, Any]
    ) -> None:
        records = self._records[visit_id]
        # Add nulls
        for item in PQ_SCHEMAS[table].names:
            if item not in record:
                record[item] = None
        # Add instance_id (for partitioning)
        record["instance_id"] = self._instance_id
        records[table].append(record)

    def _create_batch(self, visit_id: VisitId) -> None:
        """Create record batches for all records from `visit_id`"""
        if visit_id not in self._records:
            # The batch for this `visit_id` was already created, skip
            self.logger.error(
                "Trying to create batch for visit_id %d" "when one was already created",
                visit_id,
            )
            return
        for table_name, data in self._records[visit_id].items():
            try:
                df = pd.DataFrame(data)
                batch = pa.RecordBatch.from_pandas(
                    df, schema=PQ_SCHEMAS[table_name], preserve_index=False
                )
                self._batches[table_name].append(batch)
                self.logger.debug(
                    "Successfully created batch for table %s and "
                    "visit_id %s" % (table_name, visit_id)
                )
            except pa.lib.ArrowInvalid:
                self.logger.error(
                    "Error while creating record batch for table %s\n" % table_name,
                    exc_info=True,
                )
                pass

        del self._records[visit_id]

    def _is_cache_full(self) -> bool:
        for batches in self._batches.values():
            if len(batches) > CACHE_SIZE:
                return True
        return False

    async def finalize_visit_id(
        self, visit_id: VisitId, interrupted: bool = False
    ) -> None:
        if interrupted:
            await self.store_record(INCOMPLETE_VISITS, visit_id, {"visit_id": visit_id})

        # This code is pretty tricky as there are a number of things going on
        # 1. No finalize_visit_id shouldn't return unless the visit has been saved to storage
        # 2. No new batches should be created while saving out all the batches
        async with self.storing_condition:
            if self.flushing:
                # This way we wait if there is an on going flush
                await self.storing_condition.wait()
            self._create_batch(visit_id)

            if self._is_cache_full():
                self.flushing = True
                await self.flush_cache(self.storing_condition)
                self.flushing = False
            else:
                await self.storing_condition.wait()

    @abstractmethod
    async def write_table(self, table_name: TableName, table: Table) -> None:
        """Write out the table to persistent storage
        This should only return once it's actually saved out
        """

    async def flush_cache(self, cond: asyncio.Condition = None) -> None:
        """We need to hack around the fact that asyncio has no reentrant lock
        and which prevents us from creating a reentrant condition
        So we either grab the storing condition ourselves or the caller needs
        to pass us the locked storing_condition
        """
        got_cond = not not cond
        if not got_cond:
            cond = self.storing_condition
            await cond.acquire()
        assert cond.locked()
        for table_name, batches in self._batches.items():
            table = pa.Table.from_batches(batches)
            await self.write_table(table_name, table)
        cond.notify_all()

        if not got_cond:
            cond.release()