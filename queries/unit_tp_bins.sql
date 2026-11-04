-- Bins of the units of a test program (TP): what the "unit level data" Missing Bins tab compares with the rules.
-- Run by ProgramBinsService.query_units() (services/program_bins.py) with two bind variables:
--   programname  the TP name from the TP Name box (upper / lower case does not matter)
--   min_ww       first work week to look at = the work week of "today minus 60 days" (UNIT_DAYS in program_bins.py)
-- The unit level query (unit_tp_query) of the old WinForms app, cut down to the bins it needs (see Speed below), so
-- Oracle sends a few thousand rows however many units the TP has. Edit and save: the next lookup uses the new text (no
-- restart, no ';' at the end). A bind variable that is no longer in the text is simply not passed.
-- Speed: the old text (the whole old query as an inline view under an outer SELECT DISTINCT of the four bins) took 171 s for a
-- TP with 49 000 sessions in 30 days; this one takes under 1 s and returns the same rows. The ARIES tables are partitioned by
-- work week. What went:
--   * the joins to A_Device_Item (about 4.4 billion rows) and A_Device_Item_ULT_Log (about 1.8 billion): Oracle probed both
--     for every unit, and no column of them reaches the result. With them went the SELECT DISTINCT over 37 columns.
--   * UPPER() on the column: it hid the index TS_TP_FK_I (PROGRAM_NAME), so Oracle read every session of the work weeks and
--     compared the name afterwards. Every name in A_Testing_Session is upper case, so the bind is upper-cased instead (a
--     name stored in lower case would not be found).
-- What stays: "+ 0" and the hint. They make A_Testing_Session the driving table; A_Device_Testing (about 1.5 billion rows) is
-- then read through its primary key DT_PK, only in the partitions of the sessions found, not by a hash join over whole
-- partitions (that is what EXPLAIN PLAN chose with UPPER() on the column plus "dt.lao_start_ww >= min_ww": 337 million rows).
SELECT /*+ LEADING(v0 dt) USE_NL(dt) */ DISTINCT
  v0.program_name AS program_name
 ,dt.interface_bin AS interface_bin
 ,dt.functional_bin AS functional_bin
 ,dt.data_bin AS data_bin
FROM
A_Testing_Session v0
INNER JOIN A_Device_Testing dt ON v0.lao_start_ww + 0 = dt.lao_start_ww AND v0.ts_id + 0 = dt.ts_id
WHERE 1=1
 AND      v0.lao_start_ww >= :min_ww
 AND      v0.program_name = UPPER(:programname)
 AND      v0.operation In ('7899'
,'7682'
,'8748'
,'7295'
,'6970'
,'2395'
,'7777'
,'7297'
,'6508')
