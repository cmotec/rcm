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
    assert "RDP/KeyboardHook" not in unsupported  # RDM client bookkeeping, not a setting
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


RDM_PARENT_XML = r"""<?xml version="1.0" encoding="utf-8"?>
<RDMExport><Connections>
  <Connection>
    <ConnectionType>Sftp</ConnectionType>
    <CredentialConnectionGroup>0 Users</CredentialConnectionGroup>
    <CredentialConnectionSavedPath>--- Bei Verbindung abfragen ---</CredentialConnectionSavedPath>
    <Group>VMs\DMZ</Group>
    <HostSourceMode>Inherited</HostSourceMode>
    <ID>00000000-0000-0000-0000-00000000000a</ID>
    <Name>SFTP</Name>
    <ParentID>00000000-0000-0000-0000-00000000000b</ParentID>
    <Ftp><Directory>/var/lib/iso</Directory><Port>22</Port><ProxyType>HttpConnect</ProxyType>
         <ShowLocalFiles>false</ShowLocalFiles></Ftp>
    <Security><AllowOffline>False</AllowOffline><ViewOverride>Everyone</ViewOverride></Security>
  </Connection>
  <Connection>
    <ConnectionType>SSHShell</ConnectionType>
    <CredentialConnectionGroup>0 Users</CredentialConnectionGroup>
    <CredentialConnectionSavedPath>---Bei Verbindung abfragen---</CredentialConnectionSavedPath>
    <Description>sudo nano /etc/app.yaml</Description>
    <Group>VMs\DMZ</Group>
    <ID>00000000-0000-0000-0000-00000000000b</ID>
    <Name>vm0034(Mailserver)</Name>
    <Status>{B299F468-BC24-43cb-9417-4FEDFC813E7C}</Status>
    <MetaInformation><AllowMultipleConnections>True</AllowMultipleConnections><OwnerText>Owner</OwnerText></MetaInformation>
    <Terminal><Host>VM0034.example.org</Host><PrivateKeyPromptForPassPhrase>false</PrivateKeyPromptForPassPhrase>
              <ProxyType>Socks4</ProxyType></Terminal>
  </Connection>
  <Connection>
    <Url>ws0001.example.org</Url>
    <SoundHook>DoNotPlay</SoundHook>
    <ConnectionType>RDPConfigured</ConnectionType>
    <CredentialConnectionSavedPath>0 Users\Jane Admin</CredentialConnectionSavedPath>
    <Group>Other</Group>
    <ID>00000000-0000-0000-0000-00000000000c</ID>
    <Name>WS0001</Name>
  </Connection>
  <Connection>
    <ConnectionType>SSHShell</ConnectionType>
    <Group>Other</Group>
    <ID>00000000-0000-0000-0000-00000000000d</ID>
    <Name>KVM01 (KVM of SV02)</Name>
    <Terminal><AlwaysAskForPassword>true</AlwaysAskForPassword><Host>KVM01.example.org</Host></Terminal>
  </Connection>
  <Connection>
    <ConnectionType>SSHShell</ConnectionType>
    <CredentialConnectionSavedPath>0 Users\Missing Entry</CredentialConnectionSavedPath>
    <ID>00000000-0000-0000-0000-00000000000e</ID>
    <Name>10.0.0.5</Name>
    <Terminal><Host>10.0.0.5</Host></Terminal>
  </Connection>
  <Connection>
    <ConnectionType>Group</ConnectionType>
    <Group>0 Users</Group>
    <ID>00000000-0000-0000-0000-00000000000f</ID>
    <Name>0 Users</Name>
    <GroupDetails><GroupType>Credentials</GroupType></GroupDetails>
  </Connection>
  <Connection>
    <ConnectionType>Credential</ConnectionType>
    <Group>0 Users</Group>
    <ID>00000000-0000-0000-0000-000000000010</ID>
    <Name>Jane Admin</Name>
    <Credentials />
  </Connection>
</Connections></RDMExport>
""".encode()


def test_parse_parent_links_credentials_and_labels() -> None:
    parsed = parse("export.rdm", RDM_PARENT_XML)
    by_id = {i.source_id[-2:]: i for i in parsed.items}
    sftp, mail, ws, kvm, ip, group, cred = (by_id[k] for k in ("0a", "0b", "0c", "0d", "0e", "0f", "10"))

    # sub-connection: linked to its parent, inherits host and (identical) credential setting
    assert sftp.parent_source_id == mail.source_id and sftp.host is None
    assert sftp.settings == {"start_directory": "/var/lib/iso"} and sftp.port == 22
    assert sftp.credential_mode == "choose" and sftp.credential_status == "inherited"
    assert sftp.unsupported == []  # proxy type without proxy, Security, ShowLocalFiles: bookkeeping

    # label from the name, FQDN split, notes kept below the label
    assert mail.hostname == "VM0034" and mail.search_domain == "example.org" and mail.host == "VM0034.example.org"
    assert mail.description == "Mailserver\n\nsudo nano /etc/app.yaml"
    assert mail.credential_mode == "choose" and mail.credential_status == "choose_at_connect"
    assert mail.unsupported == []

    assert ws.description is None and ws.hostname == "ws0001"
    assert ws.credential_source_id == cred.source_id and ws.settings == {"disable_audio": True}
    assert kvm.description == "KVM of SV02" and kvm.credential_mode == "prompt"
    assert ip.hostname == "10.0.0.5" and ip.search_domain is None and ip.description is None
    assert ip.credential_status == "reference_missing" and ip.warnings

    assert group.kind == "folder" and group.credential_folder
    # preview order: folders, credentials, then connections by folder with children right after parents
    order = [i.source_id[-2:] for i in parsed.items]
    assert order.index("0b") + 1 == order.index("0a")
    assert order[:2] == ["0f", "10"]
