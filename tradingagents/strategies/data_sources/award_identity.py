"""Reviewed native UEIs, never recipient-name guesses.

This deliberately small crosswalk is a source-coverage limit. Add identifiers
only with primary recipient/issuer evidence; a contractor name is not an ID.
Parent links must come from the *same award's* native detail, not the recipient
profile's potentially multiple historical parents.
"""
from __future__ import annotations

import re


_BOEING_LISTING = "https://www.sec.gov/Archives/edgar/data/12927/000162828026004357/ba-20251231.htm"
_LOCKHEED_LISTING = "https://www.sec.gov/Archives/edgar/data/936468/000162828026004195/lmt-20251231.htm"
_BOEING_RECIPIENT = "https://api.usaspending.gov/api/v2/recipient/22d282c6-4508-7d30-97f1-7401ea73b12a-C/"
_LOCKHEED_AWARD = "https://api.usaspending.gov/api/v2/awards/CONT_AWD_FA868224CB001_9700_-NONE-_-NONE-/"

# Independently reviewed 2026-10-09. Recipient -> legal issuer is established
# by the issuer's own UEI publication / federal award notice. Award-specific
# parent links establish the two parent UEIs; SEC covers CIK and common stock.
VERIFIED_UEI_ISSUERS = {
    "WZWRLY4G3PL8": {
        "ticker": "BA", "cik": "0000012927", "issuer_name": "THE BOEING COMPANY",
        "provenance": [
            "https://www.boeingsuppliers.com/content/dam/boeing/boeingsuppliers/boeing-suppliers/becoming/terms/ccr/p/P1229-CCR-8-30-22.pdf",
            _BOEING_LISTING,
        ],
    },
    "NU2UC8MX6NK1": {
        "ticker": "BA", "cik": "0000012927", "issuer_name": "THE BOEING COMPANY",
        "provenance": [_BOEING_RECIPIENT, _BOEING_LISTING],
    },
    "H7PNSVNN5827": {
        "ticker": "LMT", "cik": "0000936468", "issuer_name": "LOCKHEED MARTIN CORPORATION",
        "provenance": [
            "https://sam.gov/opp/c22a155c16d6429baf021aeaf7d66c47/view",
            _LOCKHEED_LISTING,
        ],
    },
    "ZFN2JJXBLZT3": {
        "ticker": "LMT", "cik": "0000936468", "issuer_name": "LOCKHEED MARTIN CORPORATION",
        "provenance": [_LOCKHEED_AWARD, _LOCKHEED_LISTING],
    },
}


CROSSWALK_VERSION = "reviewed_2026-10-22"

# Native award detail binds UEI to the legal recipient; SEC ownership exhibits
# bind that legal recipient to the listed issuer. Names are never lookup keys.
VERIFIED_UEI_ISSUERS.update({
    "XMUZGJN98231": {
        "ticker": "UNH",
        "cik": "0000731766",
        "issuer_name": "UNITEDHEALTH GROUP INCORPORATED",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_36C10G26K0298_3600_36C79119D0006_3600/",
            "https://www.sec.gov/Archives/edgar/data/731766/000073176625000063/unhex21112312024.htm",
            "https://www.sec.gov/Archives/edgar/data/731766/000073176626000062/unh-20251231.htm"
        ]
    },
    "MT9AATHS7ZB5": {
        "ticker": "TPC",
        "cik": "0000077543",
        "issuer_name": "TUTOR PERINI CORPORATION",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70Z05026F43000039_7008_70Z04723DPCNI0004_7008/",
            "https://www.sec.gov/Archives/edgar/data/77543/000007754326000186/tpc-20260630.htm"
        ]
    },
    "RRFJZGASZJ41": {
        "ticker": "VVX",
        "cik": "0001601548",
        "issuer_name": "V2X, INC.",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_15JPSS26C00000450_1501_-NONE-_-NONE-/",
            "https://www.sec.gov/Archives/edgar/data/1601548/000160154826000015/exhibit21-subsidiaries1231.htm",
            "https://www.sec.gov/Archives/edgar/data/1601548/000110465926058322/tm2614073-1_424b5.htm"
        ]
    },
    "HFK9V1G2B513": {
        "ticker": "MSI",
        "cik": "0000068505",
        "issuer_name": "MOTOROLA SOLUTIONS, INC.",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_15BNAS26F00000249_1540_70B04C19D00000010_7014/",
            "https://www.sec.gov/Archives/edgar/data/68505/000006850526000027/msiq22026pressrelease.htm"
        ]
    },
    "J64CSQTQNRC1": {
        "ticker": "IBM",
        "cik": "0000051143",
        "issuer_name": "INTERNATIONAL BUSINESS MACHINES CORPORATION",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_28321326FDS030164_2800_28321326D00060004_2800/",
            "https://www.sec.gov/Archives/edgar/data/51143/000005114326000077/ibm-20260722xex991.htm"
        ]
    },
    "QGUQWSU5AHB4": {
        "ticker": "PRM",
        "cik": "0001880319",
        "issuer_name": "PERIMETER SOLUTIONS, INC.",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_1202SC26K2506_12C2_1202SC26T2500_12C2/",
            "https://www.sec.gov/Archives/edgar/data/1880319/000188031926000013/exhibit211123125.htm",
            "https://www.sec.gov/Archives/edgar/data/1880319/000188031926000048/prm-20260630.htm"
        ]
    },
    "C47BNA8GM833": {
        "ticker": "ACN",
        "cik": "0001467373",
        "issuer_name": "ACCENTURE PLC",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_91003126F0083_9100_HHSN316201200002W_7529/",
            "https://newsroom.accenture.com/news/2026/accenture-federal-services-wins-noaa-contract-to-modernize-national-weather-service-forecast-operations",
            "https://newsroom.accenture.com/news/2026/accenture-federal-services-and-openai-partner-to-accelerate-secure-ai-adoption-across-the-federal-government"
        ]
    },
    "DMPAKJ9N9K66": {
        "ticker": "MDLN",
        "cik": "0002046386",
        "issuer_name": "MEDLINE INC.",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_36C10X26K0559_3600_36C10X23D0032_3600/",
            "https://www.sec.gov/Archives/edgar/data/2046386/000204638626000009/ex211subsidiariesoftheregi.htm",
            "https://www.sec.gov/Archives/edgar/data/2046386/000204638626000009/mdln-20251231.htm"
        ]
    },
    "HV8BH9BPG8Y9": {
        "ticker": "LDOS",
        "cik": "0001336920",
        "issuer_name": "LEIDOS HOLDINGS, INC.",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_75N91026F00034_7529_75N91019D00024_7529/",
            "https://www.sec.gov/Archives/edgar/data/1336920/000133692026000030/ldos1022026ex21.htm",
            "https://www.sec.gov/Archives/edgar/data/1336920/000119312526058093/d75920ds3asr.htm"
        ]
    },
    "SMNWM6HN79X5": {
        "ticker": "GD",
        "cik": "0000040533",
        "issuer_name": "GENERAL DYNAMICS CORPORATION",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_1333BJ27C00280001_1344_-NONE-_-NONE-/",
            "https://www.sec.gov/Archives/edgar/data/40533/000004053326000006/ex21-20251231.htm",
            "https://www.sec.gov/Archives/edgar/data/40533/000004053326000006/gd-20251231.htm"
        ]
    },
    "JMLKZZ1NL2Z6": {
        "ticker": "GEO",
        "cik": "0000923796",
        "issuer_name": "THE GEO GROUP, INC.",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70CDCR26FR0000131_7012_70CDCR20D00000009_7012/",
            "https://www.sec.gov/Archives/edgar/data/923796/000119312526071747/geo-20251231.htm"
        ]
    }
})

VERIFIED_UEI_NO_LISTED_TARGET = {
    "J7M9HPTGJ1S9": {
        "recipient_name": "TRIWEST HEALTHCARE ALLIANCE CORP",
        "ultimate_owner": "Non-profit health plans and university hospital systems",
        "ownership_basis": "nonprofit_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_36C10G26K0300_3600_36C10G19D0038_3600/",
            "https://www.triwest.com/en/news/news-archive/2024/triwest-honored-to-begin-transition-to-tricare-west-region/",
            "https://www.triwest.com/en/about/website-terms-and-conditions/",
            "https://www.gao.gov/products/b-421405.2%2Cb-421405.3"
        ]
    },
    "VMEFT5X61JT9": {
        "recipient_name": "CROWLEY GOVERNMENT SERVICES, INC.",
        "ultimate_owner": "Crowley family and Crowley employees, through Crowley Holdings Inc.",
        "ownership_basis": "wholly_privately_family_employee_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_693JF726F00059N_6938_693JF720G000004_6938/",
            "https://www.crowley.com/company-overview/"
        ]
    },
    "GPXRWUEUHZ19": {
        "recipient_name": "M. A. MORTENSON COMPANY",
        "ultimate_owner": "Mortenson family",
        "ownership_basis": "family_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_75N99026F00002_7529_75N99019D00013_7529/",
            "https://www.mortenson.com/news-insights/ceo-announcement",
            "https://www.mortenson.com/locations/salt-lake-city/culture"
        ]
    },
    "VEP4UN7LDMK5": {
        "recipient_name": "WHITING-TURNER CONTRACTING COMPANY, THE",
        "ultimate_owner": "Whiting-Turner employees",
        "ownership_basis": "employee_owned_sar_disclosure",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70Z05026F43000038_7008_70Z04723DPCNI0006_7008/",
            "https://clark.legistar.com/View.ashx?GUID=F800EEF9-D3C6-432F-8AA1-B395690766D4&ID=13976624&M=F"
        ]
    },
    "H1KPDZLCMNR8": {
        "recipient_name": "HENSEL PHELPS CONSTRUCTION CO",
        "ultimate_owner": "Hensel Phelps employees",
        "ownership_basis": "employee_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70B01C26F00001021_7014_70B01C26D00000024_7014/",
            "https://henselphelps.com/the-hensel-phelps-way/history/",
            "https://www.henselphelps.com/careers/"
        ]
    },
    "R6CPWWDD4AM1": {
        "recipient_name": "GRUNLEY CONSTRUCTION CO., INC.",
        "ultimate_owner": "Kenneth M. Grunley and Grunley family",
        "ownership_basis": "family_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70B01C26F00001119_7014_70B01C26D00000023_7014/",
            "https://grunley.com/staff/kenneth-m-grunley/"
        ]
    },
    "SRFGXDGTHRU6": {
        "recipient_name": "MARCOM GROUP, INC",
        "ultimate_owner": "Lauren Rainford",
        "ownership_basis": "individual_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70B06C26F00001127_7014_70B06C24A00000037_7014/",
            "https://marcomgroup.com/2025/11/03/advertising-and-marketing-executive-acquires-marcom-group/",
            "https://marcomgroup.com/terms-of-use/"
        ]
    },
    "F3PQM5C4ATN8": {
        "recipient_name": "MESSER CONSTRUCTION CO",
        "ultimate_owner": "Messer Construction employee owners through ESOP",
        "ownership_basis": "employee_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_W912QR26CA035_9700_-NONE-_-NONE-/",
            "https://www.messer.com/about-messer/",
            "https://www.messer.com/about-messer/employee-ownership/",
            "https://esca.us/news/moving-from-employment-to-owning-our-future/",
            "https://esca.us/news/employee-ownership-stories-employee-ownership-empowers-kentucky-workers-from-the-jobsite-to-retirement/"
        ]
    },
    "MH2KKA8M75E9": {
        "recipient_name": "W. W. CLYDE & CO.",
        "ultimate_owner": "Clyde family through privately owned Clyde Companies",
        "ownership_basis": "family_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_140P2026C0052_1443_-NONE-_-NONE-/",
            "https://wwclyde.net/company/",
            "https://www.clydeinc.com/about/our-story/"
        ]
    },
    "DMFWBVTL9324": {
        "recipient_name": "WALSH FEDERAL LLC",
        "ultimate_owner": "Walsh family through The Walsh Group",
        "ownership_basis": "family_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_697DCK26C00269_6920_-NONE-_-NONE-/",
            "https://walshwebsiteassets.blob.core.windows.net/sitedocs/pdf/tcfdwalshgroupreportfy2025-12000.pdf",
            "https://www.walshgroup.com/news/2017.html"
        ]
    },
    "DT8KJHZXVJH5": {
        "recipient_name": "CARAHSOFT TECHNOLOGY CORP",
        "ultimate_owner": "Craig P. Abod",
        "ownership_basis": "individual_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_28321326FDX030166_2800_47QSWA18D008F_4732/",
            "https://www.cpsboe.org/content/actions/2026_05/26-0528-PR8.pdf",
            "https://www.cpsboe.org/meetings/board-actions/4436",
            "https://www.state.wv.us/admin/purchase/Bids/FY2022/B_0506_WIC2200000001_03.pdf"
        ]
    }
}

# Preserve the previous review date for earlier entries. Updating the crosswalk
# version does not re-review their ownership evidence.
for _entry in (*VERIFIED_UEI_ISSUERS.values(), *VERIFIED_UEI_NO_LISTED_TARGET.values()):
    _entry.setdefault("reviewed_on", "2026-10-10")

# Reviewed 2026-10-22 against native award details and primary ownership sources.
# Recipient entries only: stale native parents (Veritas / VF Imagewear) are not
# promoted into current-owner mappings. Lydig remains unresolved; see
# docs/verification/2026-10-22-award-source-repair.md for the evidence boundary.
VERIFIED_UEI_ISSUERS.update({
    "CZJKHV86FSB2": {
        "ticker": "OMC",
        "cik": "0000029989",
        "issuer_name": "OMNICOM GROUP INC.",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_75D30126F21224_7523_47QRAA23D0052_4732/",
            "https://www.sec.gov/Archives/edgar/data/29989/000002998926000006/omc-20251231.htm",
            "https://www.sec.gov/Archives/edgar/data/29989/000002998926000006/a2025q4exhibit21.htm",
            "https://investor.omc.com/news/news-details/2025/Omnicom-Announces-Strategy-and-Executive-Leadership-Following-Acquisition-of-Interpublic/default.aspx",
        ],
        "reviewed_on": "2026-10-22",
        "ownership_evidence": (
            "Same-award detail identifies Omnicom Group Inc. as Ketchum's parent. Omnicom's 2025 SEC Form "
            "10-K identifies Ketchum in its group and NYSE common stock OMC (CIK 29989). Exhibit 21 lists "
            "significant holding subsidiaries and does not name Ketchum."
        ),
        "ownership_bounds": "Recipient UEI only; the native parent UEI is not added as a general mapping.",
    },
})

VERIFIED_UEI_NO_LISTED_TARGET.update({
    "QQBXQR679KM9": {
        "recipient_name": "CLARK CONSTRUCTION GROUP LLC",
        "ultimate_owner": "Clark Construction Group private shareholders",
        "ownership_basis": "privately_held",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70B01C26F00000874_7014_70B01C26D00000021_7014/",
            "https://www.clarkconstruction.com/sites/default/files/2023-01/Superstructure%20Spring%202016%20Low%20Res%20PDF.pdf",
            "https://www.clarkconstruction.com/about-us",
            "https://www.clarkconstruction.com/clark-industrial",
        ],
        "reviewed_on": "2026-10-22",
        "ownership_evidence": (
            "The company's Spring 2016 newsletter documents the transfer of CEI's ownership stake to "
            "existing Clark Construction Group owners and executives at the end of 2015. Native awards "
            "identify the same legal recipient and Clark Construction LLC parent. The current Clark Industrial "
            "page explicitly describes Clark as privately held."
        ),
        "ownership_bounds": (
            "Current private status is corroborated by the company website. The transfer document is "
            "historical; no current shareholder percentages or individual "
            "holdings are asserted."
        ),
    },
    "LFH4SUCVA379": {
        "recipient_name": "ENVIRONMENTAL CHEMICAL CORPORATION",
        "ultimate_owner": "Environmental Chemical Corporation employee owners",
        "ownership_basis": "wholly_employee_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70B01C26F00001125_7014_70B01C26D00000022_7014/",
            "https://www.ecc.net/about-us/",
            "https://www.ecc.net/why-ecc/",
        ],
        "reviewed_on": "2026-10-22",
        "ownership_evidence": (
            "The company's About page identifies employee ownership. The HTML counter immediately before "
            "EMPLOYEE-OWNED has data-to-value=\"100\"; intermediate animated values are not the ownership "
            "percentage. Native award detail identifies Environmental Chemical Corporation in Burlingame, "
            "California."
        ),
        "ownership_bounds": "Current ownership review; no historical award-date ownership claim.",
    },
    "W31DQKBPJCD3": {
        "recipient_name": "SOUTHWEST VALLEY CONSTRUCTORS CO",
        "ultimate_owner": "Kiewit employee shareholders through Kiewit Infrastructure West Co.",
        "ownership_basis": "privately_employee_owned_parent",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70B01C26F00001093_7014_70B01C26D00000025_7014/",
            "https://www.gao.gov/assets/720/715122.pdf",
            "https://swvconstructors.com/",
            "https://www.kiewit.com/careers/",
        ],
        "reviewed_on": "2026-10-22",
        "ownership_evidence": (
            "GAO-21-372, printed page 20, identifies Southwest Valley Constructors as a subsidiary of "
            "Kiewit Infrastructure West. The recipient website confirms Kiewit affiliation; Kiewit's "
            "current Careers page identifies privately held employee ownership."
        ),
        "ownership_bounds": (
            "Preserve native parent EUKMSNED1371. The ownership chain beyond native detail comes from GAO "
            "and company sources."
        ),
    },
    "XXNJCWMAKNH8": {
        "recipient_name": "BL HARBERT INTERNATIONAL LLC",
        "ultimate_owner": "Harbert family through privately held BL Harbert International",
        "ownership_basis": "family_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70B01C26F00000878_7014_70B01C26D00000019_7014/",
            "https://www.linkedin.com/company/blharbert/",
            "https://harbertrealty.com/wp-content/uploads/2016/09/BL-Harbert-International-Awarded-Vesta2.pdf",
            "https://blharbert.com/about/",
        ],
        "reviewed_on": "2026-10-22",
        "ownership_evidence": (
            "The company-authored LinkedIn profile identifies family ownership and privately held status. "
            "A BL Harbert press release dated August 2, 2016 independently identifies private ownership "
            "and the same Birmingham corporate address as the native award."
        ),
        "ownership_bounds": (
            "The current family-ownership statement is company-authored; the corroborating press release "
            "is historical. Exact ownership percentages are undisclosed."
        ),
    },
    "MYEKECP4M5H4": {
        "recipient_name": "PEACHTREE LUMBER COMPANY, INC",
        "ultimate_owner": "Peachtree Lumber Company local private shareholders",
        "ownership_basis": "locally_owned_s_corporation",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_12363N27C4000_12C2_-NONE-_-NONE-/",
            "https://peachtreelumber.com/",
        ],
        "reviewed_on": "2026-10-22",
        "ownership_evidence": (
            "The company website identifies local ownership and the same Brasstown, North Carolina "
            "location as the native award. Native detail also identifies a Subchapter S Corporation and a "
            "self-parent UEI."
        ),
        "ownership_bounds": (
            "Individual shareholder names are undisclosed. Government private-sector classifications "
            "alone do not prove an unlisted company."
        ),
    },
    "LBM4GLJNDK31": {
        "recipient_name": "KALEO, INC.",
        "ultimate_owner": "Funds managed by Marathon Asset Management",
        "ownership_basis": "private_fund_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_75A50126C00010_7505_-NONE-_-NONE-/",
            "https://kaleo.com/marathon-asset-management-announces-acquisition-of-kaleo/",
            "https://ir.tredegar.com/news-releases/news-release-details/tredegar-completes-sale-ownership-interest-kaleo/",
            "https://kaleo.com/about/",
        ],
        "reviewed_on": "2026-10-22",
        "ownership_evidence": (
            "The company announced acquisition by Marathon-managed funds in November 2021. Tredegar's "
            "December 28, 2021 release confirms its ownership-interest sale closed. Kaléo's current board "
            "page identifies Marathon investment representatives."
        ),
        "ownership_bounds": (
            "Fund ownership is distinct from ownership of the fund manager. A manager acquisition does "
            "not prove direct listed-parent equity ownership of Kaléo."
        ),
    },
    "N9NJK877QJK9": {
        "recipient_name": "GUIDEHOUSE INC.",
        "ultimate_owner": "Bain Capital Private Equity investment affiliate",
        "ownership_basis": "private_equity_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_7571MN26F80170_7571_GS00F045DA_4732/",
            "https://guidehouse.com/news/corporate-news/2023/guidehouse-completes-transaction-with-bain-capital",
            "https://baincapitalprivateequity.com/portfolio",
        ],
        "reviewed_on": "2026-10-22",
        "ownership_evidence": (
            "Guidehouse's December 14, 2023 release confirms its acquisition by Bain Capital Private "
            "Equity from Veritas closed. Bain's current private equity portfolio lists Guidehouse. The "
            "native award's Veritas parent name is historical."
        ),
        "ownership_bounds": "Do not promote stale Veritas parent UEI QD34AGJ37D53 into a global Bain mapping.",
    },
    "FHKLJV1NP651": {
        "recipient_name": "SMITHS DETECTION INC.",
        "ultimate_owner": "Funds advised by CVC Capital Partners",
        "ownership_basis": "private_fund_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70T04026F7672N007_7013_70T04025D7672N004_7013/",
            "https://www.smiths.com/news-and-insights/news/2026/completion-of-smiths-detection-sale",
        ],
        "reviewed_on": "2026-10-22",
        "ownership_evidence": (
            "Smiths Group's June 30, 2026 release confirms sale of Smiths Detection to funds advised by "
            "CVC Capital Partners. The award's September 28, 2026 base obligation date follows the "
            "completed disposal."
        ),
        "ownership_bounds": (
            "Former Smiths Group ownership does not support SMIN or SMGZY attribution after the disposal. "
            "Advising the acquiring funds does not establish listed-manager equity ownership."
        ),
    },
    "TWVGJRMK6Z78": {
        "recipient_name": "BRASFIELD & GORRIE LLC",
        "ultimate_owner": "Brasfield & Gorrie private shareholders",
        "ownership_basis": "privately_held",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70B01C26F00000949_7014_70B01C26D00000020_7014/",
            "https://www.brasfieldgorrie.com/about-us/",
        ],
        "reviewed_on": "2026-10-22",
        "ownership_evidence": (
            "The company's current About Us page identifies privately held status since inception. Native "
            "award detail identifies the same Birmingham legal recipient and a self-parent UEI."
        ),
        "ownership_bounds": (
            "Private profit sharing does not prove employee equity ownership. No named shareholders or "
            "percentages are asserted."
        ),
    },
    "SQLVDBFQ4SD8": {
        "recipient_name": "WORKWEAR OUTFITTERS, LLC",
        "ultimate_owner": "Redwood Capital Investments LLC investment subsidiary",
        "ownership_basis": "private_holding_company_owned",
        "provenance": [
            "https://api.usaspending.gov/api/v2/awards/CONT_AWD_70T05026F5900N032_7013_70B01C20D00000004_7014/",
            "https://www.vfc.com/investors/news-events-presentations/press-releases/detail/1768/vf-corporation-completes-the-sale-of-the-occupational",
            "https://www.prnewswire.com/news-releases/workwear-outfitters-leader-in-workwear-apparel--footwear-begins-new-era-as-stand-alone-company-301322560.html",
            "https://wwof.com/who-we-are",
            "https://www.linkedin.com/company/workwear-outfitters",
            "https://www.stephens.com/uploads/shared/transactions-pdf/rv-retailer-deal-announcement_2018-07-02.pdf",
        ],
        "reviewed_on": "2026-10-22",
        "ownership_evidence": (
            "VF's June 28, 2021 release confirms sale to a Redwood subsidiary. Workwear Outfitters' own "
            "June 29 release confirms the former VF Workwear name and completed sale. Its current "
            "employer profile identifies privately held status and the award's Nashville address."
        ),
        "ownership_bounds": (
            "Do not map legacy VF Imagewear parent UEI H7MCPFKLJLK3 to VFC or globally to Redwood. Exact "
            "private-shareholder percentages are undisclosed."
        ),
    },
})


def native_uei(value: object) -> str:
    """UEIs are twelve uppercase alphanumeric characters, with no coercion."""
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{12}", value) else ""


def resolve_award_issuer(contract: dict) -> dict:
    """Resolve only native identifiers; never trust a supplied ticker/flag."""
    recipient = native_uei(contract.get("recipient_uei"))
    parent = (native_uei(contract.get("parent_recipient_uei"))
              if contract.get("recipient_identity_status") == "native_award_verified" else "")
    def reviewed(uei):
        if uei in VERIFIED_UEI_ISSUERS:
            return {**VERIFIED_UEI_ISSUERS[uei], "status": "verified_listed_target"}
        if uei in VERIFIED_UEI_NO_LISTED_TARGET:
            return {**VERIFIED_UEI_NO_LISTED_TARGET[uei], "status": "verified_no_listed_target", "ticker": "", "cik": ""}
        return None
    direct, ultimate = reviewed(recipient), reviewed(parent)
    if direct and ultimate and (
            direct["status"] != ultimate["status"]
            or (direct["status"] == "verified_listed_target" and direct["cik"] != ultimate["cik"])
            or (direct["status"] == "verified_no_listed_target" and direct["ultimate_owner"] != ultimate["ultimate_owner"])):
        return {"verified": False, "resolved": False, "status": "unresolved", "reason": "conflicting_recipient_issuer"}
    identity = direct or ultimate
    if not identity:
        return {"verified": False, "resolved": False, "status": "unresolved", "reason": "unverified_recipient_issuer"}
    return {**identity, "provenance": list(identity["provenance"]),
            "verified": identity["status"] == "verified_listed_target", "resolved": True,
            **({"reason": "verified_no_listed_target"} if identity["status"] == "verified_no_listed_target" else {}),
            "matched_uei": recipient if direct else parent,
            "relationship": "recipient" if direct else "parent",
            "crosswalk_version": CROSSWALK_VERSION,
            "reviewed_on": identity["reviewed_on"],
            "temporal_scope": "prospective_current_ownership",
            "relationship_source": contract.get("recipient_identity_source", "")}
