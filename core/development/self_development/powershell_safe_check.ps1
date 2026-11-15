param(
    [Parameter(Mandatory=$true)][string]$TargetFile,
    [Parameter(Mandatory=$true)][string]$WorkspaceRoot
)
$ErrorActionPreference = 'Stop'

# Safe automatic execution is intentionally narrower than the manually approved
# PowerShell policy. It permits basic workspace file creation/editing and reads,
# but no deletion, moving, copying, process launch, network, registry, credentials,
# dynamic evaluation, loops, variables, or arbitrary .NET calls.
$allowed = @(
    'Get-ChildItem','Get-Content','Set-Content','Add-Content','Out-File',
    'Test-Path','Join-Path','Split-Path','Resolve-Path','New-Item',
    'Write-Output','Write-Host','Write-Warning','Select-String','Get-FileHash',
    'Compare-Object','Measure-Object','Sort-Object','Select-Object',
    'Format-Table','Format-List','ConvertTo-Json','ConvertFrom-Json',
    'Get-Date','Get-Location','Clear-Host'
)
$fileCommands = @(
    'Get-ChildItem','Get-Content','Set-Content','Add-Content','Out-File',
    'Test-Path','New-Item','Select-String','Get-FileHash'
)
$writeCommands = @('Set-Content','Add-Content','Out-File','New-Item')
$pathParams = @('Path','LiteralPath','FilePath','ChildPath')
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $TargetFile, [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {
    $parseErrors | ForEach-Object { Write-Output ("PARSE_ERROR: " + $_.Message) }
    exit 2
}

$blockedTypes = @(
    'InvokeMemberExpressionAst','TypeExpressionAst','UsingExpressionAst',
    'ScriptBlockExpressionAst','DynamicKeywordStatementAst','FunctionDefinitionAst',
    'ForStatementAst','ForEachStatementAst','WhileStatementAst','DoWhileStatementAst',
    'DoUntilStatementAst','ClassDefinitionAst','ConfigurationDefinitionAst',
    'DataStatementAst','AssignmentStatementAst'
)
foreach ($node in $ast.FindAll({ param($n) $blockedTypes -contains $n.GetType().Name }, $true)) {
    Write-Output ("SAFE_POLICY_BLOCK: AST node not permitted: " + $node.GetType().Name)
    exit 3
}
foreach ($node in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
    Write-Output ("SAFE_POLICY_BLOCK: variables are not permitted: " + $node.Extent.Text)
    exit 4
}
foreach ($node in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.RedirectionAst] }, $true)) {
    Write-Output 'SAFE_POLICY_BLOCK: output redirection is not permitted.'
    exit 5
}

$root = [System.IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
$rootFull = $root + [System.IO.Path]::DirectorySeparatorChar
$commands = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
foreach ($cmd in $commands) {
    $name = $cmd.GetCommandName()
    if (-not $name -or $allowed -notcontains $name) {
        Write-Output ("SAFE_POLICY_BLOCK: command is not allowlisted: " + $cmd.Extent.Text)
        exit 6
    }
    $elements = @($cmd.CommandElements)
    # Never permit link targets or alternate data streams; New-Item may create only files/directories.
    for ($j = 0; $j -lt $elements.Count; $j++) {
        $paramNode = $elements[$j]
        if ($paramNode -is [System.Management.Automation.Language.CommandParameterAst]) {
            if ($paramNode.ParameterName -in @('Target','Stream')) {
                Write-Output 'SAFE_POLICY_BLOCK: target/alternate data stream parameters are not permitted.'
                exit 16
            }
            if ($name -eq 'New-Item' -and $paramNode.ParameterName -in @('ItemType','Type')) {
                $typeAst = $paramNode.Argument
                if (-not $typeAst -and ($j + 1) -lt $elements.Count) { $typeAst = $elements[$j + 1] }
                if (-not ($typeAst -is [System.Management.Automation.Language.StringConstantExpressionAst]) -or [string]$typeAst.Value -notin @('File','Directory')) {
                    Write-Output 'SAFE_POLICY_BLOCK: New-Item is limited to File or Directory; links/devices are forbidden.'
                    exit 17
                }
            }
        }
    }
    for ($i = 0; $i -lt $elements.Count; $i++) {
        $element = $elements[$i]
        if ($element -is [System.Management.Automation.Language.CommandParameterAst]) {
            if ($pathParams -contains $element.ParameterName) {
                $valueAst = $element.Argument
                if (-not $valueAst -and ($i + 1) -lt $elements.Count) {
                    $valueAst = $elements[$i + 1]
                    $i++
                }
                if (-not ($valueAst -is [System.Management.Automation.Language.StringConstantExpressionAst])) {
                    Write-Output ("SAFE_POLICY_BLOCK: path argument must be a literal relative path: " + $cmd.Extent.Text)
                    exit 7
                }
                $value = [string]$valueAst.Value
                if ([System.IO.Path]::IsPathRooted($value) -or $value -match '(^|[\\/])\.\.([\\/]|$)' -or $value -match '^[A-Za-z]:' -or $value.Contains(':')) {
                    Write-Output ("SAFE_POLICY_BLOCK: path escapes workspace or uses alternate stream: " + $value)
                    exit 8
                }
                if ($name -in $writeCommands -and $value -match '[*?\[\]]') {
                    Write-Output ("SAFE_POLICY_BLOCK: wildcard writes are not permitted: " + $value)
                    exit 9
                }
                foreach ($component in ($value -split '[\\/]')) {
                    if ($component.EndsWith('.') -or $component.EndsWith(' ')) {
                        Write-Output ("SAFE_POLICY_BLOCK: path component ends with dot/space: " + $value)
                        exit 18
                    }
                    $device = ($component -split '\.')[0]
                    if ($device -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') {
                        Write-Output ("SAFE_POLICY_BLOCK: reserved Windows device path: " + $value)
                        exit 10
                    }
                }
                $candidate = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($root, $value))
                if ($candidate -ne $root -and -not $candidate.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
                    Write-Output ("SAFE_POLICY_BLOCK: path escapes workspace: " + $value)
                    exit 11
                }
                $relative = $candidate.Substring($root.Length).TrimStart([System.IO.Path]::DirectorySeparatorChar)
                $cursor = $root
                foreach ($part in ($relative -split '[\\/]')) {
                    if ([string]::IsNullOrWhiteSpace($part)) { continue }
                    $cursor = [System.IO.Path]::Combine($cursor, $part)
                    if (Test-Path -LiteralPath $cursor) {
                        $item = Get-Item -LiteralPath $cursor -Force
                        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                            Write-Output ("SAFE_POLICY_BLOCK: reparse point in workspace path: " + $cursor)
                            exit 12
                        }
                    }
                }
                if ($writeCommands -contains $name -and (Test-Path -LiteralPath $candidate)) {
                    Write-Output ("SAFE_POLICY_BLOCK: automatic PowerShell may create new files/directories only; overwrites need reviewed approval: " + $value)
                    exit 19
                }
                if ($writeCommands -contains $name -and (Test-Path -LiteralPath $candidate)) {
                    Write-Output ("SAFE_POLICY_BLOCK: automatic PowerShell may create new files/directories only; existing targets need a reviewed approval: " + $value)
                    exit 19
                }
            }
        }
        elseif ($element -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
            continue
        }
        elseif ($element -is [System.Management.Automation.Language.CommandParameterAst]) {
            continue
        }
        elseif ($element -is [System.Management.Automation.Language.StringExpandableExpressionAst]) {
            if ($element.NestedExpressions.Count -gt 0) {
                Write-Output 'SAFE_POLICY_BLOCK: expandable strings with variables are not permitted.'
                exit 13
            }
        }
        else {
            if ($element -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) {
                Write-Output ("SAFE_POLICY_BLOCK: dynamic command argument not permitted: " + $element.Extent.Text)
                exit 14
            }
        }
    }
    if ($fileCommands -contains $name) {
        $hasPath = $false
        foreach ($el in $elements) {
            if ($el -is [System.Management.Automation.Language.CommandParameterAst] -and $pathParams -contains $el.ParameterName) { $hasPath = $true }
        }
        if (-not $hasPath) {
            Write-Output ("SAFE_POLICY_BLOCK: file command requires an explicit workspace-relative path: " + $name)
            exit 15
        }
    }
}
Write-Output 'SAFE_POLICY_OK: allowlisted non-destructive PowerShell; paths constrained to workspace.'
exit 0
