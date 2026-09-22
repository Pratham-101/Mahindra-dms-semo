-- =====================================================================
--  OnlineDMS — Vehicle Invoice slice
--  Mock schema for the Dealer BOT POC
--
--  Table and column names are taken VERBATIM from the SQL in the
--  Vehicle Invoice SOP (revised, 8 Sep 2026). Where the SOP does not name
--  a column, it is marked  -- [QK]  so the OEM can correct it against the real
--  OnlineDMS schema. Nothing here is invented silently.
--
--  Dialect: T-SQL (SQL Server), because OnlineDMS is SQL Server.
--  A SQLite build of the same tables is produced by db/build_sqlite.py for
--  local running; see README.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Dealer master.  SOP: SELECT STATE_ID,* FROM MDMS_DEALER WHERE DEALER_ID=13111
-- ---------------------------------------------------------------------
CREATE TABLE dbo.MDMS_DEALER (
    DEALER_ID     INT          NOT NULL,
    BRANCH_ID     INT          NOT NULL,
    STATE_ID      VARCHAR(10)  NOT NULL,          -- SOP sample: 'KAR'
    DEALER_NAME   VARCHAR(120) NULL,              -- [QK]
    ACTIVE        BIT          NOT NULL DEFAULT 1,-- [QK]
    CONSTRAINT PK_MDMS_DEALER PRIMARY KEY (DEALER_ID, BRANCH_ID)
);

-- ---------------------------------------------------------------------
-- Customer master.  Customer TYPE drives Scenario 6 (CSD vs Individual).
-- The SOP names the CSD/Individual split but not the column, so this is [QK].
-- ---------------------------------------------------------------------
CREATE TABLE dbo.MDMS_CUSTOMER (
    CUSTOMER_ID    BIGINT       NOT NULL PRIMARY KEY,
    CUSTOMER_NAME  VARCHAR(120) NULL,             -- [QK]
    CUSTOMER_TYPE  VARCHAR(20)  NOT NULL,         -- [QK] 'CSD' | 'INDIVIDUAL' | 'CORPORATE'
    MOBILE_NO      VARCHAR(15)  NULL,             -- [QK]
    DEALER_ID      INT          NOT NULL,
    BRANCH_ID      INT          NOT NULL
);

-- ---------------------------------------------------------------------
-- Model / part mapping.
-- SOP: SELECT * FROM MDMS_MODEL_PART WHERE PART_ID='KE190260DB'
-- ---------------------------------------------------------------------
CREATE TABLE dbo.MDMS_MODEL_PART (
    PART_ID     VARCHAR(30) NOT NULL PRIMARY KEY, -- SOP sample: 'KE190260DB'
    MODEL_ID    VARCHAR(30) NOT NULL,             -- SOP sample: '000030000300000029'
    MODEL_DESC  VARCHAR(120) NULL                 -- [QK]
);

-- ---------------------------------------------------------------------
-- Booking -> part.
-- SOP: SELECT * FROM MDMS_BOOKING_PART WHERE BOOKING_ID=26307 AND DEALER_ID=13111 AND BRANCH_ID=1
-- ---------------------------------------------------------------------
CREATE TABLE dbo.MDMS_BOOKING_PART (
    BOOKING_ID  BIGINT      NOT NULL,
    DEALER_ID   INT         NOT NULL,
    BRANCH_ID   INT         NOT NULL,
    PART_ID     VARCHAR(30) NOT NULL,
    CONSTRAINT PK_MDMS_BOOKING_PART PRIMARY KEY (BOOKING_ID, DEALER_ID, BRANCH_ID),
    CONSTRAINT FK_BOOKING_PART_MODEL FOREIGN KEY (PART_ID) REFERENCES dbo.MDMS_MODEL_PART(PART_ID)
);

-- ---------------------------------------------------------------------
-- EMPS subsidy (PM E-Drive).  This is the table that killed the "fixed 5000"
-- assumption — the amount is per MODEL_ID + STATE_ID, and eligibility is
-- simply "an ACTIVE row exists".
-- SOP: SELECT EMPS,* FROM MDMS_MODEL_SUBSIDY WHERE MODEL_ID='...' AND STATE_ID='KAR' AND ACTIVE=1
-- ---------------------------------------------------------------------
CREATE TABLE dbo.MDMS_MODEL_SUBSIDY (
    MODEL_ID  VARCHAR(30)   NOT NULL,
    STATE_ID  VARCHAR(10)   NOT NULL,
    EMPS      DECIMAL(12,2) NOT NULL,
    ACTIVE    BIT           NOT NULL DEFAULT 1,
    CONSTRAINT PK_MDMS_MODEL_SUBSIDY PRIMARY KEY (MODEL_ID, STATE_ID)
);

-- ---------------------------------------------------------------------
-- RTO master. Named by the SOP only through the FK error in Scenario 7:
--   'The UPDATE statement conflicted with the FOREIGN KEY constraint "fk_rto_book"
--    ... table "dbo.MDMS_RTO"'
-- Columns are [QK] — the SOP never lists them.
-- ---------------------------------------------------------------------
CREATE TABLE dbo.MDMS_RTO (
    RTO_ID    INT          NOT NULL PRIMARY KEY,  -- [QK]
    RTO_CODE  VARCHAR(20)  NOT NULL,              -- [QK]
    RTO_NAME  VARCHAR(120) NULL,                  -- [QK]
    STATE_ID  VARCHAR(10)  NOT NULL,              -- [QK]
    ACTIVE    BIT          NOT NULL DEFAULT 1     -- [QK]
);

-- ---------------------------------------------------------------------
-- Vehicle price master. Scenario 6: "Please go to Vehicle Price Master and
-- create the price." Table name is [QK] — the SOP names the SCREEN, not the table.
-- ---------------------------------------------------------------------
CREATE TABLE dbo.MDMS_VEHICLE_PRICE_MASTER (
    MODEL_ID       VARCHAR(30)   NOT NULL,
    STATE_ID       VARCHAR(10)   NOT NULL,
    CUSTOMER_TYPE  VARCHAR(20)   NOT NULL,        -- [QK] 'CSD' | 'INDIVIDUAL'
    PRICE          DECIMAL(12,2) NOT NULL,
    ACTIVE         BIT           NOT NULL DEFAULT 1,
    CONSTRAINT PK_MDMS_VEH_PRICE PRIMARY KEY (MODEL_ID, STATE_ID, CUSTOMER_TYPE)
);

-- ---------------------------------------------------------------------
-- Vehicle invoice — the centre of this use case.
-- SOP: SELECT DISC_VALUE, BOOKING_ID, * FROM MDMS_VEHICLE_INVOICE
--      WHERE DEALER_ID=13111 AND INVOICE_NO=' 1152344'
--        AND CAST(INVOICE_DATE AS DATE)='09/07/2026'
-- NOTE the leading space in the SOP's INVOICE_NO literal — see README,
-- the API trims and the mock stores untrimmed on purpose.
-- ---------------------------------------------------------------------
CREATE TABLE dbo.MDMS_VEHICLE_INVOICE (
    VEH_INVOICE_ID     BIGINT        NOT NULL PRIMARY KEY,  -- [QK]
    DEALER_ID          INT           NOT NULL,
    BRANCH_ID          INT           NOT NULL,
    INVOICE_NO         VARCHAR(30)   NOT NULL,
    INVOICE_DATE       DATETIME      NOT NULL,
    BOOKING_ID         BIGINT        NULL,
    CUSTOMER_ID        BIGINT        NULL,                   -- [QK]
    DISC_VALUE         DECIMAL(12,2) NULL,
    CRM_REF_CUST_CODE  VARCHAR(40)   NULL,   -- the EMR / referral code
    RTO_ID             INT           NULL,                   -- [QK] drives fk_rto_book
    GST_TYPE           VARCHAR(10)   NULL,   -- [QK] 'IGST' | 'CGST_SGST'
    INVOICE_STATUS     TINYINT       NULL,                   -- [QK]
    ACTIVE             BIT           NOT NULL DEFAULT 1,     -- [QK]
    CONSTRAINT FK_INV_RTO FOREIGN KEY (RTO_ID) REFERENCES dbo.MDMS_RTO(RTO_ID)
);

CREATE INDEX IX_VEH_INVOICE_LOOKUP  ON dbo.MDMS_VEHICLE_INVOICE (DEALER_ID, INVOICE_NO, INVOICE_DATE);
CREATE INDEX IX_VEH_INVOICE_EMR     ON dbo.MDMS_VEHICLE_INVOICE (CRM_REF_CUST_CODE);


-- ---------------------------------------------------------------------
-- [QK] Accountability tables. Not in the SOP — added because the bot is a
-- non-human caller and "which dealer did this on whose behalf" has to be
-- answerable after the fact, not reconstructed from logs.
-- ---------------------------------------------------------------------
CREATE TABLE dbo.MDMS_API_AUDIT (
    AUDIT_ID        BIGINT IDENTITY(1,1) PRIMARY KEY,
    CALLED_AT       DATETIME     NOT NULL DEFAULT GETDATE(),
    CLIENT_ID       VARCHAR(60)  NULL,      -- which service credential (e.g. 'devrev-dealer-bot')
    DEALER_ID       INT          NULL,
    BRANCH_ID       INT          NULL,
    USER_ID         BIGINT       NULL,
    ENDPOINT        VARCHAR(120) NOT NULL,
    IDEMPOTENCY_KEY VARCHAR(80)  NULL,
    OUTCOME         VARCHAR(30)  NOT NULL,  -- OK | UNAUTHORIZED | REPLAYED | ERROR
    DETAIL          VARCHAR(400) NULL
);

-- Replay protection for writes. A retry carrying the same key returns the
-- ORIGINAL result and performs no second write.
CREATE TABLE dbo.MDMS_API_IDEMPOTENCY (
    IDEMPOTENCY_KEY VARCHAR(80)  NOT NULL PRIMARY KEY,
    ENDPOINT        VARCHAR(120) NOT NULL,
    DEALER_ID       INT          NOT NULL,
    REQUEST_HASH    VARCHAR(64)  NOT NULL,
    RESPONSE_JSON   NVARCHAR(MAX) NOT NULL,
    CREATED_AT      DATETIME     NOT NULL DEFAULT GETDATE()
);
