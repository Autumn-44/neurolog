param([Parameter(Mandatory=$true)][string]$WonderlustRoot)
$ErrorActionPreference = 'Stop'
$wonderlustRoot = (Resolve-Path -LiteralPath $WonderlustRoot).Path
$appPath = Join-Path $wonderlustRoot 'app.js'
$appText = [IO.File]::ReadAllText($appPath)
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'neurolog.js') -Destination (Join-Path $wonderlustRoot 'utils/neurolog.js')
if (!$appText.Contains("require('./utils/neurolog')")) {
    $appText = $appText.Replace('const app = express();', "const app = express();`nconst neurolog = require('./utils/neurolog').createLogger();`napp.use(neurolog.middleware);")
    $appText = $appText.Replace('app.use((err, req, res, next) => {', "app.use(neurolog.errorMiddleware);`napp.use((err, req, res, next) => {")
    $appText = $appText.Replace("console.log('Connected to MongoDB');", "console.log('Connected to MongoDB');`n    neurolog.log('Database connection established');")
    $appText = $appText.Replace('const server = app.listen', "neurolog.log('Wonderlust application starting');`n    const server = app.listen")
    $appText = $appText.Replace("console.error('Database connection failed. Check ATLASDB_URL and Atlas network access.', err.code || err.name);", "neurolog.log('Database connection failed', 3);`n    console.error('Database connection failed. Check ATLASDB_URL and Atlas network access.', err.code || err.name);")
    [IO.File]::WriteAllText($appPath, $appText)
}
Write-Output 'Installed Wonderlust request, error, and database event logging.'
