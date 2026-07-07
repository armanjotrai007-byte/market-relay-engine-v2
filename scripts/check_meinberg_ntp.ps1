param(
    [string]$NtpServiceName,
    [string]$NtpqPath,
    [string]$NtpConfigPath,
    [string]$ExpectedUpstream,
    [double]$MaxOffsetMilliseconds,
    [switch]$SelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Check {
    param(
        [bool]$Passed,
        [string]$Name,
        [string]$Detail
    )
    $prefix = if ($Passed) { "PASS" } else { "FAIL" }
    Write-Output ("[{0}] {1}: {2}" -f $prefix, $Name, $Detail)
}

function ConvertTo-DoubleInvariant {
    param([string]$Text)
    $value = 0.0
    $ok = [double]::TryParse(
        $Text,
        [System.Globalization.NumberStyles]::Float,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$value
    )
    if (-not $ok -or [double]::IsNaN($value) -or [double]::IsInfinity($value)) {
        throw "not a finite numeric value"
    }
    return $value
}

function ConvertTo-IntInvariant {
    param([string]$Text)
    $value = 0
    $ok = [int]::TryParse(
        $Text,
        [System.Globalization.NumberStyles]::Integer,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$value
    )
    if (-not $ok) {
        throw "not an integer value"
    }
    return $value
}

function Parse-NtpqPeers {
    param(
        [string]$Text,
        [double]$ThresholdMilliseconds
    )
    if ($ThresholdMilliseconds -lt 0 -or [double]::IsNaN($ThresholdMilliseconds) -or [double]::IsInfinity($ThresholdMilliseconds)) {
        throw "MaxOffsetMilliseconds must be finite and non-negative"
    }

    $selected = @()
    foreach ($line in ($Text -split "`r?`n")) {
        $trimmed = $line.Trim()
        if ($trimmed.Length -eq 0 -or $trimmed.StartsWith("remote") -or $trimmed.StartsWith("=")) {
            continue
        }
        if (-not $trimmed.StartsWith("*")) {
            continue
        }
        $parts = $trimmed -split "\s+"
        if ($parts.Count -lt 10) {
            throw "selected ntpq peer line has too few columns"
        }
        $reach = ConvertTo-IntInvariant $parts[6]
        $offset = ConvertTo-DoubleInvariant $parts[8]
        $selected += [pscustomobject]@{
            Peer = $parts[0].Substring(1)
            Reach = $reach
            OffsetMilliseconds = $offset
            ThresholdMilliseconds = $ThresholdMilliseconds
        }
    }

    if ($selected.Count -ne 1) {
        throw ("expected exactly one selected peer, found {0}" -f $selected.Count)
    }
    $peer = $selected[0]
    if ($peer.Reach -eq 0) {
        throw "selected peer reach is zero"
    }
    if ([math]::Abs($peer.OffsetMilliseconds) -gt $ThresholdMilliseconds) {
        throw "selected peer offset exceeds threshold"
    }
    return $peer
}

function Invoke-SelfTest {
    $valid = @"
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
*203.0.113.10    .GPS.            1 u   17   64   377    1.234   -0.456   0.020
+203.0.113.11    .GPS.            1 u   12   64   377    1.111    0.100   0.010
"@
    $noneSelected = @"
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
+203.0.113.10    .GPS.            1 u   17   64   377    1.234   -0.456   0.020
"@
    $zeroReach = @"
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
*203.0.113.10    .GPS.            1 u   17   64     0    1.234   -0.456   0.020
"@
    $overThreshold = @"
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
*203.0.113.10    .GPS.            1 u   17   64   377    1.234   25.000   0.020
"@

    $failed = $false
    try {
        $peer = Parse-NtpqPeers -Text $valid -ThresholdMilliseconds 5.0
        Write-Check $true "self-test valid selected peer" ("reach={0}; offset_ms={1}; threshold_ms={2}" -f $peer.Reach, $peer.OffsetMilliseconds, $peer.ThresholdMilliseconds)
    } catch {
        Write-Check $false "self-test valid selected peer" "parser rejected valid fixture"
        $failed = $true
    }

    foreach ($case in @(
        @{ Name = "self-test no selected peer"; Text = $noneSelected },
        @{ Name = "self-test zero reach"; Text = $zeroReach },
        @{ Name = "self-test over threshold"; Text = $overThreshold }
    )) {
        try {
            $null = Parse-NtpqPeers -Text $case.Text -ThresholdMilliseconds 5.0
            Write-Check $false $case.Name "parser accepted invalid fixture"
            $failed = $true
        } catch {
            Write-Check $true $case.Name "parser rejected invalid fixture"
        }
    }

    if ($failed) {
        exit 1
    }
    exit 0
}

function Require-Parameter {
    param(
        [string]$Value,
        [string]$Name
    )
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is required"
    }
}

if ($SelfTest) {
    if ($PSBoundParameters.ContainsKey("NtpServiceName") -or
        $PSBoundParameters.ContainsKey("NtpqPath") -or
        $PSBoundParameters.ContainsKey("NtpConfigPath") -or
        $PSBoundParameters.ContainsKey("ExpectedUpstream") -or
        $PSBoundParameters.ContainsKey("MaxOffsetMilliseconds")) {
        Write-Check $false "mode" "-SelfTest cannot be combined with server validation parameters"
        exit 1
    }
    Invoke-SelfTest
}

$failedNormal = $false
try {
    Require-Parameter $NtpServiceName "NtpServiceName"
    Require-Parameter $NtpqPath "NtpqPath"
    Require-Parameter $NtpConfigPath "NtpConfigPath"
    Require-Parameter $ExpectedUpstream "ExpectedUpstream"
    if (-not $PSBoundParameters.ContainsKey("MaxOffsetMilliseconds") -or $MaxOffsetMilliseconds -lt 0 -or [double]::IsNaN($MaxOffsetMilliseconds) -or [double]::IsInfinity($MaxOffsetMilliseconds)) {
        throw "MaxOffsetMilliseconds is required and must be finite and non-negative"
    }
} catch {
    Write-Check $false "parameters" $_.Exception.Message
    exit 1
}

try {
    $service = Get-Service -Name $NtpServiceName -ErrorAction Stop
    $serviceRunning = $service.Status -eq "Running"
    Write-Check $serviceRunning "service status" ("{0} is {1}" -f $NtpServiceName, $service.Status)
    if (-not $serviceRunning) { $failedNormal = $true }
} catch {
    Write-Check $false "service status" "service not found"
    $failedNormal = $true
}

try {
    $ntpqItem = Get-Item -LiteralPath $NtpqPath -ErrorAction Stop
    $ntpqOk = $ntpqItem.PSIsContainer -eq $false -and $ntpqItem.Extension -ieq ".exe"
    Write-Check $ntpqOk "ntpq executable" $ntpqItem.FullName
    if (-not $ntpqOk) { $failedNormal = $true }
} catch {
    Write-Check $false "ntpq executable" "path not found"
    $failedNormal = $true
}

try {
    $configItem = Get-Item -LiteralPath $NtpConfigPath -ErrorAction Stop
    $configOk = $configItem.PSIsContainer -eq $false
    Write-Check $configOk "configuration file" $configItem.FullName
    if (-not $configOk) { $failedNormal = $true }
    $configText = Get-Content -LiteralPath $configItem.FullName -Raw -Encoding UTF8
    $upstreamMatch = $configText.Contains($ExpectedUpstream)
    Write-Check $upstreamMatch "configuration upstream-token match" $ExpectedUpstream
    if (-not $upstreamMatch) { $failedNormal = $true }
} catch {
    Write-Check $false "configuration upstream-token match" "configuration check failed"
    $failedNormal = $true
}

if (-not $failedNormal) {
    try {
        $ntpqOutput = & $NtpqPath -pn 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "ntpq exited non-zero"
        }
        $peer = Parse-NtpqPeers -Text ($ntpqOutput -join "`n") -ThresholdMilliseconds $MaxOffsetMilliseconds
        Write-Check $true "selected-peer existence" $peer.Peer
        Write-Check $true "selected-peer reach" ("reach={0}" -f $peer.Reach)
        Write-Check $true "selected-peer offset" ("offset_ms={0}; threshold_ms={1}" -f $peer.OffsetMilliseconds, $peer.ThresholdMilliseconds)
    } catch {
        Write-Check $false "selected-peer validation" $_.Exception.Message
        $failedNormal = $true
    }
}

if ($failedNormal) {
    exit 1
}
exit 0
