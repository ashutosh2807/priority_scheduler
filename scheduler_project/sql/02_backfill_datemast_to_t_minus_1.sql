-- DATEMAST backfill for the missing September 2026 report dates.
-- Run in Oracle SQL Developer as the DATEMAST owner (F5 / Run Script).
-- Prepared on 11-Sep-2026: default output is 9 rows, 01-Sep to 10-Sep.
-- Existing rows, including future 30-Sep / 01-Oct rows, are preserved.
-- No COMMIT is issued automatically. Review the results before committing.
--
-- Start is explicit: older DATEMAST gaps can represent real holidays.
-- End follows the India operating date minus one day (T-1).
-- Add circle-specific holidays below; an absent DATEMAST row cannot itself
-- distinguish an unloaded working date from a real holiday during backfill.
-- Previous/next dates follow the same exclusions. Period ends remain calendar
-- boundaries (March 31 financial year), matching the existing column values.

INSERT INTO DATEMAST (
    REPORT_DATE, M_H_PREV_DAY, M_H_YEAR,
    M_H_QUARTER, M_H_MONTH, M_H_FUT_DATE
)
WITH
parameters AS (
    SELECT DATE '2026-09-01' AS start_date,
           TRUNC(CAST(SYSTIMESTAMP AT TIME ZONE 'Asia/Kolkata' AS DATE)) - 1
               AS end_date
    FROM dual
),
additional_holidays (holiday_date) AS (
    SELECT CAST(NULL AS DATE) FROM dual WHERE 1 = 0
    -- Add actual additional holidays as rows here, for example:
    -- UNION ALL SELECT DATE 'YYYY-MM-DD' FROM dual
    -- Include known upcoming holidays when calculating M_H_FUT_DATE too.
),
day_numbers AS (
    -- Include a year of lookahead for the next-working-date reference only.
    -- The INSERT itself is restricted to end_date; future rows are not added.
    SELECT LEVEL - 1 AS day_offset
    FROM dual
    CONNECT BY LEVEL <= (
        SELECT GREATEST(0, end_date - start_date + 1) + 366 FROM parameters
    )
),
calendar_dates AS (
    SELECT p.start_date + n.day_offset AS report_date
    FROM parameters p CROSS JOIN day_numbers n
),
working_dates AS (
    SELECT c.report_date
    FROM calendar_dates c
    WHERE c.report_date - TRUNC(c.report_date, 'IW') <> 6 -- Sunday
      AND NOT (
          c.report_date - TRUNC(c.report_date, 'IW') = 5 -- Saturday
          AND CEIL((c.report_date - TRUNC(c.report_date, 'MM') + 1) / 7)
              IN (2, 4)
      )
      AND NOT EXISTS (
          SELECT 1 FROM additional_holidays h
          WHERE TRUNC(h.holiday_date) = c.report_date
      )
    UNION
    -- Preserve known exceptional working days already published in DATEMAST.
    SELECT TRUNC(d.REPORT_DATE)
    FROM DATEMAST d CROSS JOIN parameters p
    WHERE d.REPORT_DATE >= p.start_date AND d.REPORT_DATE < p.end_date + 1
    UNION
    -- Use the actual previous published date as the opening anchor.
    SELECT MAX(TRUNC(d.REPORT_DATE))
    FROM DATEMAST d CROSS JOIN parameters p
    WHERE d.REPORT_DATE < p.start_date
    HAVING COUNT(*) > 0
),
neighbours AS (
    SELECT report_date,
           LAG(report_date) OVER (ORDER BY report_date) AS previous_working_date,
           LEAD(report_date) OVER (ORDER BY report_date) AS next_working_date
    FROM working_dates
)
SELECT n.report_date,
       n.previous_working_date,
       CAST(CASE
           WHEN n.report_date >= TRUNC(n.report_date, 'YYYY')
                                     + INTERVAL '3' MONTH - 1
           THEN TRUNC(n.report_date, 'YYYY') + INTERVAL '3' MONTH - 1
           ELSE TRUNC(n.report_date, 'YYYY') - INTERVAL '9' MONTH - 1
       END AS DATE) AS m_h_year,
       TRUNC(n.report_date + 1, 'Q') - 1 AS m_h_quarter,
       TRUNC(n.report_date + 1, 'MM') - 1 AS m_h_month,
       n.next_working_date
FROM neighbours n CROSS JOIN parameters p
WHERE n.report_date BETWEEN p.start_date AND p.end_date
  AND NOT EXISTS (
      SELECT 1 FROM DATEMAST d
      WHERE d.REPORT_DATE >= n.report_date AND d.REPORT_DATE < n.report_date + 1
  );

-- Review the rows in this session, then run COMMIT separately to save them.
SELECT REPORT_DATE, M_H_PREV_DAY, M_H_YEAR,
       M_H_QUARTER, M_H_MONTH, M_H_FUT_DATE
FROM DATEMAST
WHERE REPORT_DATE >= DATE '2026-09-01'
  AND REPORT_DATE < TRUNC(CAST(SYSTIMESTAMP AT TIME ZONE 'Asia/Kolkata' AS DATE))
ORDER BY REPORT_DATE;

-- COMMIT;
-- Or ROLLBACK; to discard this session's uncommitted inserts.
