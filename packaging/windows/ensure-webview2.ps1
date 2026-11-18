# Only used by disposable CI runners; the user installer explains prerequisites.
$ErrorActionPreference = 'Stop'
$key = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$installed = $false
foreach ($hive in @([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryHive]::CurrentUser)) {
    $root = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, [Microsoft.Win32.RegistryView]::Registry32)
    $client = $root.OpenSubKey($key)
    if ($client) {
        $version = $client.GetValue('pv')
        if ($version -and $version -ne '0.0.0.0') { $installed = $true }
        $client.Dispose()
    }
    $root.Dispose()
}
if (-not $installed) {
    $installer = Join-Path $env:RUNNER_TEMP 'MicrosoftEdgeWebview2Setup.exe'
    Invoke-WebRequest 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $installer
    $signature = Get-AuthenticodeSignature $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation(?:,|$)') {
        throw 'WebView2 installer did not have a valid Microsoft signature.'
    }
    $process = Start-Process $installer -ArgumentList '/silent', '/install' -PassThru -Wait
    if ($process.ExitCode -notin @(0, 3010)) { throw "WebView2 install failed: $($process.ExitCode)" }
}
