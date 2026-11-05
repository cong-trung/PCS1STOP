-- Bins a test program (TP) defines: what the Missing Bins tab compares with the rules.
-- Run by ProgramBinsService.query() (services/program_bins.py) with one bind variable, once on every database of oracle.alias
-- (config.json: VN.ARIES and SVC.ARIES) - the text must run on all of them; the rows are merged, each bin once:
--   programname  the TP name from the TP Name box (exact match)
-- The Test Program query of the old WinForms app (querybutton_Click in Form1.cs). Edit and save: the next lookup uses the
-- new text (no restart, no ';' at the end). The service reads PROGRAM_NAME and BIN_NUMBER, other columns are left out.
SELECT DISTINCT atp.program_name AS program_name, atpb.description AS bin_description, atpb.bin_number AS bin_number
FROM A_Test_Program atp, A_Test_Program_Bin atpb
WHERE atp.data_domain <> 'ETEST' AND atp.program_name = atpb.program_name AND atp.program_name = :programname
