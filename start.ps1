# Start the actual application in the current terminal. Arguments remain argv items.
$launcherPath = Join-Path $PSScriptRoot 'run.py'
$interpreterPath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $interpreterPath -PathType Leaf)) {
    $interpreterPath = Join-Path $PSScriptRoot '.venv/bin/python'
}
if (-not (Test-Path -LiteralPath $interpreterPath -PathType Leaf)) {
    $pythonCommand = Get-Command python, python3 -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $pythonCommand) {
        [Console]::Error.WriteLine('Python was not found. Install the environment described in README.md.')
        exit 1
    }
    $interpreterPath = $pythonCommand.Source
}
& $interpreterPath $launcherPath @args
exit $LASTEXITCODE

