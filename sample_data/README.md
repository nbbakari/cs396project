# Sample data

Files for trying epaData without a CAMPD API key or access to the team's
shared database. Load them on the **File Intake & Validation** page (`/upload`):
drag a file in, review the validation report, then **Approve import**.

| File | Rows | What it shows |
| --- | --- | --- |
| `campd_annual_emissions_2024.csv` | 4,024 | Every unit in the U.S. for 2024, as returned by the CAMPD API (`emissions/apportioned/annual` merged with `facilities/attributes`). Enough for every explorer search. |
| `campd_annual_emissions_KY_2015-2020.csv` | 408 | Kentucky for 2015, 2018, 2019 and 2020 in one file: a multi-year upload, and history on the unit pages once 2024 is loaded too. |
| `sample_with_errors.csv` | 10 | A small file with one example of each validation rule (see the comments below). Two rows are imported with warnings, one is imported cleanly, and seven are rejected and listed in the data-quality report. |

The CAMPD files are public U.S. EPA data from Clean Air Markets Program Data
(<https://campd.epa.gov>), saved by the app's own retrieval in October 2026.
They were retrieved before owner/operator was added, so that column is blank
for them; a live retrieval fills it.

`sample_with_errors.csv`, row by row (row 1 is the header):

| Row | Problem | Result |
| --- | --- | --- |
| 2 | Same facility, unit and year as row 8 | rejected (`duplicate_in_file`) |
| 3 | Blank metrics | imported, metrics recorded as 0 (`defaulted_to_zero` warning) |
| 4 | Blank facility ID | rejected (`missing_key`) |
| 5 | Year `abc` | rejected (`invalid_value`) |
| 6 | Negative gross load | rejected (`negative_value`) |
| 7 | 9,000 operating hours (more than a year has) | rejected (`out_of_range`) |
| 8 | Valid | imported |
| 9 | Year 1990, before CAMPD reporting began | rejected (`out_of_range`) |
| 10 | State `Kentucky` instead of `KY` | imported, state left blank (`invalid_value` warning) |
| 11 | Gross load `lots` | rejected (`invalid_value`) |
