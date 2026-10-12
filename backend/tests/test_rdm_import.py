"""Devolutions RDM export parsing."""

from __future__ import annotations

from app.services.importers.rdm import parse, split_host_port

RDM_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<ArrayOfConnection>
  <Connection>
    <ConnectionType>Group</ConnectionType>
    <Group>Customers\\ACME</Group>
    <ID>11111111-1111-1111-1111-111111111111</ID>
    <Name>ACME</Name>
  </Connection>
  <Connection>
    <ConnectionType>RDPConfigured</ConnectionType>
    <Group>Customers\\ACME</Group>
    <ID>22222222-2222-2222-2222-222222222222</ID>
    <Name>ACME DC01</Name>
    <Url>dc01.acme.local:3390</Url>
    <Description>Domain controller</Description>
    <Keywords>prod "domain controller"</Keywords>
    <CredentialConnectionID>44444444-4444-4444-4444-444444444444</CredentialConnectionID>
    <RDP>
      <AuthentificationLevel>ConnectDontWarnMe</AuthentificationLevel>
      <UseCredSSP>true</UseCredSSP>
      <SafePassword>AQAAANCMnd8BFdERjHoAwE/Cl+sBAAAA</SafePassword>
      <UserName>administrator</UserName>
      <Domain>ACME</Domain>
      <KeyboardHook>OnTheRemoteComputer</KeyboardHook>
    </RDP>
    <SubConnections>
      <Connection>
        <ConnectionType>SSHShell</ConnectionType>
        <ID>33333333-3333-3333-3333-333333333333</ID>
        <Name>DC01 SSH</Name>
        <Terminal>
          <Host>dc01.acme.local</Host>
          <Port>2222</Port>
          <Username>root</Username>
          <PrivateKeyPath>C:\\keys\\id_rsa</PrivateKeyPath>
        </Terminal>
      </Connection>
    </SubConnections>
  </Connection>
  <Connection>
    <ConnectionType>Credential</ConnectionType>
    <ID>44444444-4444-4444-4444-444444444444</ID>
    <Name>ACME admin</Name>
    <Credentials><UserName>administrator</UserName><Domain>ACME</Domain></Credentials>
  </Connection>
  <Connection>
    <ConnectionType>WebBrowser</ConnectionType>
    <ID>55555555-5555-5555-5555-555555555555</ID>
    <Name>Portal</Name>
    <Url>https://portal.acme.local</Url>
  </Connection>
  <Connection>
    <ConnectionType>FTP</ConnectionType>
    <ID>66666666-6666-6666-6666-666666666666</ID>
    <Name>Files</Name>
    <Url>ftp.acme.local</Url>
    <FTP><Encryption>ImplicitTLS</Encryption><PassiveMode>false</PassiveMode></FTP>
  </Connection>
</ArrayOfConnection>
"""


def test_parse_xml_export() -> None:
    parsed = parse("export.rdm", RDM_XML)
    by_name = {i.name: i for i in parsed.items}
    assert by_name["ACME"].kind == "folder" and by_name["ACME"].folder_path == ["Customers", "ACME"]

    dc = by_name["ACME DC01"]
    assert dc.protocol == "rdp" and dc.host == "dc01.acme.local" and dc.port == 3390
    assert dc.folder_path == ["Customers", "ACME"]
    assert dc.tags == ["prod", "domain controller"]
    assert dc.credential_status == "encrypted_not_migrated"
    assert dc.credential_source_id == "44444444-4444-4444-4444-444444444444"
    assert dc.settings == {"cert_policy": "ignore", "security": "nla"}
    assert dc.username == "administrator" and dc.domain == "ACME"
    unsupported = {u["setting"]: u["value"] for u in dc.unsupported}
    assert unsupported.get("RDP/KeyboardHook") == "OnTheRemoteComputer"  # reported, not dropped
    assert all("AQAAAN" not in v for v in unsupported.values())  # encrypted value never echoed

    ssh = by_name["DC01 SSH"]
    assert ssh.protocol == "ssh" and ssh.parent_source_id == dc.source_id and ssh.port == 2222
    assert any("private key file" in w for w in ssh.warnings)

    assert by_name["ACME admin"].kind == "credential"
    assert by_name["Portal"].kind == "unsupported"
    ftp = by_name["Files"]
    assert ftp.protocol == "ftps" and ftp.settings == {"mode": "active", "tls_mode": "implicit"}

    summary = parsed.summary()
    assert summary["unsupported_types"] == {"WebBrowser": 1}
    assert summary["encrypted_credentials"] == 1


def test_parse_csv_export() -> None:
    data = (
        b"Name,ConnectionType,Group,Host,Port,Username,Password,Extra Column\n"
        b"web01,SSHShell,Linux\\Web,web01.example.com,22,deploy,,something\n"
        b"win01,RDPConfigured,Windows,win01.example.com:3391,,admin,secret,\n"
    )
    parsed = parse("export.csv", data)
    web, win = parsed.items
    assert web.protocol == "ssh" and web.folder_path == ["Linux", "Web"] and web.username == "deploy"
    assert web.unsupported == [{"setting": "Extra Column", "value": "something"}]
    assert win.port == 3391 and win.credential_status == "encrypted_not_migrated"


def test_invalid_xml_is_reported_not_raised() -> None:
    parsed = parse("x.rdm", b"<ArrayOfConnection><Connection>")
    assert parsed.items == [] and parsed.errors


def test_xml_entities_are_refused() -> None:
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ArrayOfConnection>&a;</ArrayOfConnection>'
    parsed = parse("x.rdm", evil)
    assert parsed.items == [] and parsed.errors


def test_split_host_port() -> None:
    assert split_host_port("host:3390") == ("host", 3390)
    assert split_host_port("rdp://user@host.example:22/x") == ("host.example", 22)
    assert split_host_port("[fe80::1]:22") == ("fe80::1", 22)
    assert split_host_port("fe80::1") == ("fe80::1", None)
