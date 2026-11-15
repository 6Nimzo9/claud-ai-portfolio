param(
    [Parameter(Mandatory=$true)][string]$TargetFile,
    [Parameter(Mandatory=$true)][string]$WorkspaceRoot
)
$ErrorActionPreference = 'Stop'

$allowed = @(
    'Get-ChildItem','Get-Content','Set-Content','Add-Content','Out-File',
    'Test-Path','Join-Path','Split-Path','Resolve-Path','New-Item',
    'Copy-Item','Move-Item','Remove-Item','Write-Output','Write-Host',
    'Write-Warning','Select-String','Get-FileHash','Compare-Object',
    'Measure-Object','Sort-Object','Select-Object','Format-Table',
    'Format-List','ConvertTo-Json','ConvertFrom-Json','Get-Date','Get-Location',
    'Clear-Host'
)
$fileCommands = @(
    'Get-ChildItem','Get-Content','Set-Content','Add-Content','Out-File',
    'Test-Path','New-Item','Copy-Item','Move-Item','Remove-Item',
    'Select-String','Get-FileHash'
)
$pathParams = @('Path','LiteralPath','Destination','FilePath','Target','ChildPath')
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
    'AssignmentStatementAst','ForStatementAst','ForEachStatementAst',
    'WhileStatementAst','DoWhileStatementAst','DoUntilStatementAst',
    'ClassDefinitionAst','ConfigurationDefinitionAst','DataStatementAst'
)
foreach ($node in $ast.FindAll({ param($n) $blockedTypes -contains $n.GetType().Name }, $true)) {
    Write-Output ("POLICY_BLOCK: AST node not permitted: " + $node.GetType().Name)
    exit 3
}

# Disallow variable expansion and dynamic command construction. Claud's scripts
# must use literal arguments and the workspace as the current directory.
foreach ($node in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
    Write-Output ("POLICY_BLOCK: variable expression is not permitted: " + $node.Extent.Text)
    exit 4
}
foreach ($node in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.RedirectionAst] }, $true)) {
    Write-Output 'POLICY_BLOCK: output redirection is not permitted.'
    exit 5
}

$rootFull = [System.IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
$commands = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
foreach ($cmd in $commands) {
    $name = $cmd.GetCommandName()
    if (-not $name -or $allowed -notcontains $name) {
        Write-Output ("POLICY_BLOCK: command is not allowlisted: " + $cmd.Extent.Text)
        exit 6
    }

    $elements = @($cmd.CommandElements)
    # Never permit link targets or alternate data streams; New-Item may create only files/directories.
    for ($j = 0; $j -lt $elements.Count; $j++) {
        $paramNode = $elements[$j]
        if ($paramNode -is [System.Management.Automation.Language.CommandParameterAst]) {
            if ($paramNode.ParameterName -in @('Target','Stream')) {
                Write-Output 'POLICY_BLOCK: target/alternate data stream parameters are not permitted.'
                exit 16
            }
            if ($name -eq 'New-Item' -and $paramNode.ParameterName -in @('ItemType','Type')) {
                $typeAst = $paramNode.Argument
                if (-not $typeAst -and ($j + 1) -lt $elements.Count) { $typeAst = $elements[$j + 1] }
                if (-not ($typeAst -is [System.Management.Automation.Language.StringConstantExpressionAst]) -or [string]$typeAst.Value -notin @('File','Directory')) {
                    Write-Output 'POLICY_BLOCK: New-Item is limited to File or Directory; links/devices are forbidden.'
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
                    Write-Output ("POLICY_BLOCK: path argument must be a literal relative path: " + $cmd.Extent.Text)
                    exit 7
                }
                $value = [string]$valueAst.Value
                if ([System.IO.Path]::IsPathRooted($value) -or $value -match '(^|[\\/])\.\.([\\/]|$)' -or $value -match '^[A-Za-z]:' -or $value.Contains(':')) {
                    Write-Output ("POLICY_BLOCK: path escapes workspace: " + $value)
                    exit 8
                }
                foreach ($component in ($value -split '[\\/]')) {
                    if ($component.EndsWith('.') -or $component.EndsWith(' ')) {
                        Write-Output ("POLICY_BLOCK: path component ends with dot/space: " + $value)
                        exit 18
                    }
                    $device = ($component -split '\.')[0]
                    if ($device -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') {
                        Write-Output ("POLICY_BLOCK: reserved Windows device path: " + $value)
                        exit 10
                    }
                }
                $candidate = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($rootFull, $value))
                if ($candidate -ne $rootFull.TrimEnd([System.IO.Path]::DirectorySeparatorChar) -and -not $candidate.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
                    Write-Output ("POLICY_BLOCK: path escapes workspace: " + $value)
                    exit 9
                }
                # Do not follow symlinks/junctions under the workspace: a junction could
                # redirect an otherwise relative path outside the permitted tree.
                $walk = $rootFull.TrimEnd([System.IO.Path]::DirectorySeparatorChar)
                $relative = $candidate.Substring($walk.Length).TrimStart([System.IO.Path]::DirectorySeparatorChar)
                $cursor = $walk
                foreach ($part in ($relative -split '[\\/]')) {
                    if ([string]::IsNullOrWhiteSpace($part)) { continue }
                    $cursor = [System.IO.Path]::Combine($cursor, $part)
                    if (Test-Path -LiteralPath $cursor) {
                        $item = Get-Item -LiteralPath $cursor -Force
                        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                            Write-Output ("POLICY_BLOCK: reparse point in workspace path: " + $cursor)
                            exit 15
                        }
                    }
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
                Write-Output 'POLICY_BLOCK: expandable strings with variables are not permitted.'
                exit 10
            }
        }
        elseif ($element -is [System.Management.Automation.Language.CommandAst]) {
            Write-Output 'POLICY_BLOCK: nested command expressions are not permitted.'
            exit 11
        }
        else {
            # Command-name elements, static numbers and other AST forms are rejected.
            if ($element -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) {
                Write-Output ("POLICY_BLOCK: dynamic command argument not permitted: " + $element.Extent.Text)
                exit 12
            }
        }
    }

    if ($fileCommands -contains $name) {
        $hasPath = $false
        foreach ($el in $elements) {
            if ($el -is [System.Management.Automation.Language.CommandParameterAst] -and $pathParams -contains $el.ParameterName) {
                $hasPath = $true
            }
        }
        if (-not $hasPath) {
            Write-Output ("POLICY_BLOCK: file command requires an explicit workspace-relative path: " + $name)
            exit 13
        }
        if ($name -in @('Copy-Item','Move-Item')) {
            $hasDestination = $false
            foreach ($el in $elements) {
                if ($el -is [System.Management.Automation.Language.CommandParameterAst] -and $el.ParameterName -eq 'Destination') { $hasDestination = $true }
            }
            if (-not $hasDestination) {
                Write-Output ("POLICY_BLOCK: " + $name + " requires an explicit -Destination inside the workspace.")
                exit 14
            }
        }
    }
}
Write-Output 'POLICY_OK: static PowerShell commands only; filesystem path arguments are workspace-relative.'
exit 0
