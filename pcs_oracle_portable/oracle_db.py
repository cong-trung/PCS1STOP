"""Oracle access for PCS 1Stop - the Python side of ``query.cs`` and of the two Oracle ``*.Section.xsd`` files.

    C# (PCS1Stop.csproj / App.config / query.cs)               Python (this file)
    Oracle.*.Configuration.Section.xsd - Visual Studio checks  validate_oracle_section()  lxml checks the same block
      <oracle.manageddataaccess.client> in App.config            against the same xsd
    <dataSources><dataSource alias= descriptor=/>, <settings>  read_oracle_section()      alias -> descriptor, TNS_ADMIN
    OracleDataAccess(...).GetData(sql, "lotname", value)       OracleDataAccess(...).get_data(sql, "lotname", value)
    Data Source=VN.ARIES;User Id=/;   (Kerberos / NTS login)   username "/" = external auth = python-oracledb Thick mode

Thin mode (the default) has no external / Kerberos authentication and ignores sqlnet.ora, so ``User Id=/`` needs Thick
mode: an Oracle Client folder passed as ``client_lib_dir``.  The C# app uses the 11.2.0.4 x64 client (Oracle.DataAccess
4.112.4.0 from client_k64).  python-oracledb 4.0+ / 26.x dropped clients before 19c, so with an 11.2 client pin
``oracledb<4`` (3.x supports 11.2+; tested with an installed 11.2.0.1 x64 client, ORACLE_HOME env var not needed);
otherwise install Instant Client 19+.  A real user + password works in Thin mode.  Only the database calls need
``oracledb`` (the one package to install; lxml is only for the xsd check of an App.config that has the block).

``User Id=/`` against VN.ARIES is Kerberos (the repo's sqlnet.ora).  Its ``SQLNET.KERBEROS5_CONF`` holds the path of the PC
it was written on (a wrong path = ORA-12641), so ``user_config_dir()`` gives python-oracledb a per-user copy of the folder
(in %TEMP%) whose sqlnet.ora names this PC's krb5.conf: found under the user's profile, in the folder itself, or given as
``krb5_conf=`` / env PCS_KRB5_CONF.  The repo's files are never changed.  A ``tns_admin`` folder without tnsnames.ora gives
ORA-12154.

Another PC: copy this file, ``database_config`` (plus a krb5.conf in it when SQLPathFinder3 is not installed there) and,
without an Oracle client installed, the client as an ``oracle_client`` folder next to this file (``make_portable.py``
builds the whole set; or set env ORACLE_CLIENT_LIB); then ``pip install "oracledb<4"`` (64-bit Python), run the self-test.

This file only opens the connection (``connect()``) and executes a SQL string (``get_data()``).  No SQL lives here: an
app keeps its queries in files of its own and passes the text in, so the folder can move to another project as it is.

Self-test (xsd check, Kerberos login, optional SQL file):  ``python oracle_db.py [--sql FILE --bind NAME=VALUE ...]``
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, NamedTuple

HERE = Path(__file__).resolve().parent
APP_CONFIG = HERE / "App.config"
SECTION_XSD = HERE / "Oracle.ManagedDataAccess.Client.Configuration.Section.xsd"  # includes the ...Common... xsd next to it
TNS_ADMIN = HERE / "database_config"  # tnsnames.ora + sqlnet.ora
SECTION = "oracle.manageddataaccess.client"
KRB5_CONF_LINE = re.compile(r"^(\s*SQLNET\.KERBEROS5_CONF\s*=\s*)(.*?)\s*$", re.IGNORECASE)
USER_PROFILE_PATH = re.compile(r"^[A-Za-z]:\\Users\\[^\\]+\\(.+)$", re.IGNORECASE)


class OracleSection(NamedTuple):
    settings: dict[str, str]  # <settings><setting name= value=/>  (TNS_ADMIN, ...)
    data_sources: dict[str, str]  # <dataSources><dataSource alias= descriptor=/>


class QueryResult(NamedTuple):
    """What query.cs returns as a DataTable: column names + rows."""

    columns: list[str]
    rows: list[tuple[Any, ...]]

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


def _section(config_path: str | Path) -> ET.Element | None:
    if not Path(config_path).is_file():
        return None
    return ET.parse(str(config_path)).getroot().find(SECTION)


def validate_oracle_section(config_path: str | Path = APP_CONFIG, xsd_path: str | Path = SECTION_XSD) -> list[str]:
    """Schema errors of the <oracle.manageddataaccess.client> block (Visual Studio's squiggles); [] = valid or no block.

    Needs lxml, but only when the block exists.
    """
    if _section(config_path) is None:
        return []
    from lxml import etree

    section = etree.parse(str(config_path)).getroot().find(SECTION)
    schema = etree.XMLSchema(etree.parse(str(xsd_path)))
    schema.validate(section)
    return [f"line {e.line}: {e.message}" for e in schema.error_log]


def read_oracle_section(config_path: str | Path = APP_CONFIG) -> OracleSection:
    """<settings> and <dataSources> of the block.  Every <version number=...> element is merged, later entries win."""
    section = _section(config_path)
    if section is None:
        return OracleSection({}, {})
    settings = {s.get("name"): s.get("value") for s in section.findall("version/settings/setting")}
    sources = {d.get("alias"): d.get("descriptor", "").strip() for d in section.findall("version/dataSources/dataSource")}
    return OracleSection(settings, sources)


def find_krb5_conf(configured: str = "", beside: str | Path | None = None) -> Path | None:
    """This PC's krb5.conf: ``configured`` moved into the current user's profile, then the usual places (``beside`` = the
    sqlnet.ora folder, so a copied folder can carry its own); None if absent."""
    candidates = []
    moved = USER_PROFILE_PATH.match(configured)
    if moved:
        candidates.append(Path.home() / moved.group(1))
    candidates.append(Path.home() / "My Programs" / "SQLPathFinder3" / "Schema" / "krb5.conf")
    if os.environ.get("KRB5_CONFIG"):
        candidates.append(Path(os.environ["KRB5_CONFIG"]))
    if beside:
        candidates.append(Path(beside) / "krb5.conf")
    candidates.append(Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "MIT" / "Kerberos5" / "krb5.ini")
    candidates.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "krb5.ini")
    return next((path for path in candidates if path.is_file()), None)


def find_client_lib() -> str | None:
    """Folder with the Oracle client libraries (oci.dll): env ORACLE_CLIENT_LIB, else ``oracle_client\\bin`` next to this
    file (see make_portable.py), else None (python-oracledb then looks on PATH)."""
    if os.environ.get("ORACLE_CLIENT_LIB"):
        return os.environ["ORACLE_CLIENT_LIB"]
    bundled = HERE / "oracle_client" / "bin"
    return str(bundled) if (bundled / "oci.dll").is_file() else None


def user_config_dir(tns_admin: str | Path, krb5_conf: str | Path | None = None, *, must_exist: bool = True) -> str:
    """``tns_admin``, or a per-user copy of it whose sqlnet.ora names a krb5.conf that exists on this PC.

    The repo's sqlnet.ora holds the krb5.conf path of the PC it was written on (ORA-12641 anywhere else).  The folder is
    never changed: the copy lives in %TEMP%\\pcs1stop_tns\\<hash>, the same folder for the same content (python-oracledb
    wants identical init arguments on every call).  ``krb5_conf`` / env PCS_KRB5_CONF force one file; otherwise a path that
    exists is kept and a missing one is replaced by ``find_krb5_conf()``.  ``must_exist``: raise FileNotFoundError when no
    krb5.conf is found, instead of returning ``tns_admin`` unchanged.
    """
    source = Path(tns_admin)
    sqlnet = next((p for p in source.iterdir() if p.is_file() and p.name.lower() == "sqlnet.ora"), None) if source.is_dir() else None
    if sqlnet is None:
        return str(tns_admin)
    forced = krb5_conf or os.environ.get("PCS_KRB5_CONF")
    if forced and not Path(forced).is_file():
        raise FileNotFoundError(f"krb5.conf not found: {forced}")

    lines = sqlnet.read_bytes().decode("latin-1").splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        found = KRB5_CONF_LINE.match(body)
        if not found:
            continue
        configured = found.group(2).strip('"')
        if forced:
            wanted = str(forced)
        elif Path(configured).is_file():
            continue
        else:
            moved = find_krb5_conf(configured, source)
            if moved is None:
                if must_exist:
                    raise FileNotFoundError(f"{sqlnet}: SQLNET.KERBEROS5_CONF = {configured} is not on this PC and no krb5.conf "
                                            "was found - pass krb5_conf=... or set PCS_KRB5_CONF")
                continue
            wanted = str(moved)
        if wanted.lower() != configured.lower():
            lines[i] = found.group(1) + wanted + line[len(body):]
            changed = True
    if not changed:
        return str(tns_admin)

    new_sqlnet = "".join(lines).encode("latin-1")
    others = sorted((p for p in source.iterdir() if p.is_file() and p != sqlnet), key=lambda p: p.name.lower())
    digest = hashlib.sha1(new_sqlnet)
    for p in others:
        stat = p.stat()
        digest.update(f"{p.name}|{stat.st_size}|{stat.st_mtime_ns}".encode())
    target = Path(tempfile.gettempdir()) / "pcs1stop_tns" / digest.hexdigest()[:16]
    if not target.is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="tns_", dir=target.parent))
        for p in others:
            shutil.copyfile(p, staging / p.name)
        (staging / sqlnet.name).write_bytes(new_sqlnet)
        try:
            staging.rename(target)
        except OSError:  # another process created the same folder first
            shutil.rmtree(staging, ignore_errors=True)
            if not target.is_dir():
                raise
    return str(target)


class OracleDataAccess:
    """query.cs: ``OracleDataAccess("VN.ARIES").get_data(sql, "lotname", "X1")`` runs ``sql`` with bind variable ``:lotname``.

    The alias is looked up in App.config's <dataSources> first; otherwise it goes to python-oracledb as is
    (tnsnames.ora in ``tns_admin``, or an Easy Connect string).  ``tns_admin``: argument > App.config TNS_ADMIN > database_config.
    ``connect()`` hands python-oracledb the per-user copy of that folder (``config_dir``, see ``user_config_dir()``);
    ``krb5_conf`` forces a krb5.conf.
    """

    def __init__(self, tns_alias: str = "VN.ARIES", username: str = "/", password: str = "",
                 tns_admin: str | Path | None = None, *, client_lib_dir: str | None = None,
                 krb5_conf: str | Path | None = None, config_path: str | Path = APP_CONFIG):
        section = read_oracle_section(config_path)
        self.username, self.password, self.krb5_conf = username, password, krb5_conf
        self.client_lib_dir = client_lib_dir or find_client_lib()
        self.tns_admin = str(tns_admin or section.settings.get("TNS_ADMIN") or TNS_ADMIN)
        self.config_dir = self.tns_admin  # connect() swaps in the per-user copy when the Kerberos path needs fixing
        self.dsn = section.data_sources.get(tns_alias, tns_alias)

    def connect(self) -> Any:
        import oracledb

        external = self.username in ("", "/")
        self.config_dir = user_config_dir(self.tns_admin, self.krb5_conf, must_exist=external)
        if external:
            home = Path(self.client_lib_dir).parent if self.client_lib_dir else None
            if home and (home / "oracore").is_dir():  # a full client's bin: it reads timezone / message / NLS data from its home
                os.environ.setdefault("ORACLE_HOME", str(home))
            # same arguments on every call: python-oracledb allows repeated init_oracle_client() only then
            oracledb.init_oracle_client(lib_dir=self.client_lib_dir, config_dir=self.config_dir)
            return oracledb.connect(dsn=self.dsn)
        return oracledb.connect(user=self.username, password=self.password, dsn=self.dsn, config_dir=self.config_dir)

    def get_data(self, query: str, parameter_name: str | None = None, parameter_value: Any = None) -> QueryResult:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(query, {parameter_name: parameter_value} if parameter_name else {})
            return QueryResult([column[0] for column in cursor.description], cursor.fetchall())


ERROR_HINTS = {
    "DPI-1047": "Oracle client (oci.dll) not found: put it in oracle_client\\bin next to oracle_db.py, or pass --client-lib",
    "DPI-1050": 'this Oracle client is older than 19c: pip install "oracledb<4"',
    "ORA-12154": "tnsnames.ora not found: pass --tns-admin <folder with tnsnames.ora>",
    "ORA-12641": "Kerberos setup failed: check SQLNET.KERBEROS5_CONF, or pass --krb5-conf",
    "ORA-01017": "login refused: no Kerberos ticket (run klist) or no DB account for it",
}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Check App.config, the Kerberos setup and the Oracle login; --sql also "
                                                 "executes a SQL file.")
    parser.add_argument("--alias", default="VN.ARIES", help="TNS alias (default: %(default)s)")
    parser.add_argument("--sql", metavar="FILE", help="SQL file to execute after the login (a ';' at its end is dropped)")
    parser.add_argument("--bind", action="append", default=[], metavar="NAME=VALUE",
                        help="bind variable of that SQL, its value passed as text (repeat for more)")
    parser.add_argument("--client-lib", help="folder with oci.dll (default: env ORACLE_CLIENT_LIB, oracle_client\\bin next to "
                                             "this file, else PATH)")
    parser.add_argument("--tns-admin", help="folder with tnsnames.ora + sqlnet.ora (default: database_config)")
    parser.add_argument("--krb5-conf", help="krb5.conf to use (default: found for this user)")
    args = parser.parse_args(argv)
    if any("=" not in item for item in args.bind):
        parser.error("--bind needs NAME=VALUE")

    try:
        errors = validate_oracle_section()
        print("App.config <oracle.manageddataaccess.client>:", "valid" if not errors else "; ".join(errors))
    except ImportError:
        print("App.config <oracle.manageddataaccess.client>: xsd check skipped (pip install lxml)")
    db = OracleDataAccess(args.alias, tns_admin=args.tns_admin, client_lib_dir=args.client_lib, krb5_conf=args.krb5_conf)
    try:
        started = time.perf_counter()
        with db.connect() as connection:
            print(f"login OK   {args.alias}: Oracle Database {connection.version} [{time.perf_counter() - started:.1f}s]")
            import oracledb

            copy = "" if db.config_dir == db.tns_admin else f" (per-user copy of {db.tns_admin})"
            client = ".".join(map(str, oracledb.clientversion()))
            print(f"oracledb   {oracledb.__version__} thick, Oracle client {client} from {db.client_lib_dir or 'PATH'}")
            print(f"tns config {db.config_dir}{copy}")
            if args.sql:
                started = time.perf_counter()
                sql = Path(args.sql).read_text(encoding="utf-8-sig").strip().removesuffix(";").rstrip()
                cursor = connection.cursor()
                cursor.execute(sql, dict(item.partition("=")[::2] for item in args.bind))
                columns, rows = [column[0] for column in cursor.description], cursor.fetchall()
                print(f"query OK   {Path(args.sql).name}: {len(rows)} rows {columns} [{time.perf_counter() - started:.1f}s]")
                for row in rows[:5]:
                    print("          ", row)
    except Exception as error:  # noqa: BLE001 - CLI top level: print the error and a hint instead of a traceback
        message = " ".join(str(error).split())
        hint = next((text for code, text in ERROR_HINTS.items() if code in message), None)
        print(f"FAILED     {type(error).__name__}: {message}" + (f"\n           hint: {hint}" if hint else ""))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
