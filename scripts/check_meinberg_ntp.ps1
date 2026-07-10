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

function Normalize-NtpEndpointToken {
    param([string]$Token)
    $trimmed = $Token.Trim()
    if ($trimmed.EndsWith(".")) {
        return $trimmed.Substring(0, $trimmed.Length - 1)
    }
    return $trimmed
}

function Get-ActiveNtpUpstreamTokens {
    param([string]$Text)

    $tokens = @()
    foreach ($line in ($Text -split "`r?`n")) {
        $withoutComment = $line
        $commentIndex = $withoutComment.IndexOf("#")
        if ($commentIndex -ge 0) {
            $withoutComment = $withoutComment.Substring(0, $commentIndex)
        }
        $trimmed = $withoutComment.Trim()
        if ($trimmed.Length -eq 0) {
            continue
        }

        $parts = $trimmed -split "\s+"
        if ($parts.Count -lt 2) {
            continue
        }
        $directive = $parts[0]
        if (-not $directive.Equals("server", [System.StringComparison]::OrdinalIgnoreCase) -and
            -not $directive.Equals("pool", [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        $tokens += $parts[1]
    }
    return $tokens
}

function Test-ExpectedUpstreamActive {
    param(
        [string]$Text,
        [string]$ExpectedUpstream
    )
    $expected = Normalize-NtpEndpointToken $ExpectedUpstream
    foreach ($token in (Get-ActiveNtpUpstreamTokens $Text)) {
        $normalized = Normalize-NtpEndpointToken $token
        if ($normalized.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
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
        $tally = $trimmed.Substring(0, 1)
        if ($tally -ne "*" -and $tally -ne "o") {
            continue
        }
        $parts = $trimmed -split "\s+"
        if ($parts.Count -lt 10) {
            throw "selected ntpq peer line has too few columns"
        }
        if ($parts[0].Length -lt 2) {
            throw "selected ntpq peer line is missing a peer name"
        }
        $reach = ConvertTo-IntInvariant $parts[6]
        $offset = ConvertTo-DoubleInvariant $parts[8]
        $selected += [pscustomobject]@{
            Peer = $parts[0].Substring(1)
            Marker = $tally
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
    $failed = $false
    foreach ($case in @(
        @{
            Name = "self-test active server upstream";
            Text = "server expected.example.net iburst";
            Expected = "expected.example.net";
            ShouldMatch = $true
        },
        @{
            Name = "self-test active pool upstream";
            Text = "pool expected.example.net iburst";
            Expected = "expected.example.net";
            ShouldMatch = $true
        },
        @{
            Name = "self-test active upstream trailing dot";
            Text = "server expected.example.net. iburst";
            Expected = "expected.example.net";
            ShouldMatch = $true
        },
        @{
            Name = "self-test commented upstream ignored";
            Text = "# server expected.example.net iburst";
            Expected = "expected.example.net";
            ShouldMatch = $false
        },
        @{
            Name = "self-test inline comment upstream ignored";
            Text = "server other.example.net iburst # expected.example.net";
            Expected = "expected.example.net";
            ShouldMatch = $false
        },
        @{
            Name = "self-test unrelated directive ignored";
            Text = "restrict expected.example.net";
            Expected = "expected.example.net";
            ShouldMatch = $false
        },
        @{
            Name = "self-test substring hostname ignored";
            Text = "server not-expected.example.net";
            Expected = "expected.example.net";
            ShouldMatch = $false
        },
        @{
            Name = "self-test different active upstream ignored";
            Text = "server other.example.net iburst";
            Expected = "expected.example.net";
            ShouldMatch = $false
        }
    )) {
        $matched = Test-ExpectedUpstreamActive -Text $case.Text -ExpectedUpstream $case.Expected
        if ($matched -eq $case.ShouldMatch) {
            Write-Check $true $case.Name "parser result matched expectation"
        } else {
            Write-Check $false $case.Name "parser result did not match expectation"
            $failed = $true
        }
    }

    $validStar = @"
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
*203.0.113.10    .GPS.            1 u   17   64   377    1.234   -0.456   0.020
+203.0.113.11    .GPS.            1 u   12   64   377    1.111    0.100   0.010
"@
    $validPps = @"
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
o127.127.22.0    .PPS.            0 l   11   16   377    0.000    0.003   0.001
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
    $twoSelected = @"
     remote           refid      st t when poll reach   delay   offset  jitter
==============================================================================
*203.0.113.10    .GPS.            1 u   17   64   377    1.234   -0.456   0.020
o127.127.22.0    .PPS.            0 l   11   16   377    0.000    0.003   0.001
"@

    foreach ($case in @(
        @{ Name = "self-test valid star selected peer"; Text = $validStar },
        @{ Name = "self-test valid PPS selected peer"; Text = $validPps }
    )) {
        try {
            $peer = Parse-NtpqPeers -Text $case.Text -ThresholdMilliseconds 5.0
            Write-Check $true $case.Name ("marker={0}; reach={1}; offset_ms={2}; threshold_ms={3}" -f $peer.Marker, $peer.Reach, $peer.OffsetMilliseconds, $peer.ThresholdMilliseconds)
        } catch {
            Write-Check $false $case.Name "parser rejected valid fixture"
            $failed = $true
        }
    }

    foreach ($case in @(
        @{ Name = "self-test no selected peer"; Text = $noneSelected },
        @{ Name = "self-test zero reach"; Text = $zeroReach },
        @{ Name = "self-test over threshold"; Text = $overThreshold },
        @{ Name = "self-test two selected peers"; Text = $twoSelected }
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
    $upstreamMatch = Test-ExpectedUpstreamActive -Text $configText -ExpectedUpstream $ExpectedUpstream
    if ($upstreamMatch) {
        Write-Check $true "configuration expected upstream active" $ExpectedUpstream
    } else {
        Write-Check $false "configuration expected upstream missing" $ExpectedUpstream
    }
    if (-not $upstreamMatch) { $failedNormal = $true }
} catch {
    Write-Check $false "configuration expected upstream missing" "configuration check failed"
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
