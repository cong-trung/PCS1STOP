from pathlib import Path

import polars as pl
import PyUber

sql = Path(r"queries\program_bins.sql").read_text(encoding="utf-8")
datasources = ("SVC_PROD_ARIES", "VN_PROD_ARIES")
parameters = {"programname": "EMRR1X3B13K2634A"}
schema_overrides = {
    "PROGRAM_NAME": pl.String,
    "BIN_DESCRIPTION": pl.String,
    "BIN_NUMBER": pl.Int32,
}

frames = []
for datasource in datasources:
    with PyUber.connect(datasource=datasource) as conn:
        df = pl.read_database(
            query=sql,
            connection=conn,
            execute_options={"parameters": parameters},
            # PyUber's cursor metadata is not DB-API-shaped; provide Polars dtypes.
            schema_overrides=schema_overrides,
        )

    print(f"{datasource}: {df.height} rows")
    frames.append(df.with_columns(pl.lit(datasource).alias("DATASOURCE")))

results = pl.concat(frames, how="vertical")
print(results.head(5).rows())
