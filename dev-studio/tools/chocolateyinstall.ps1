$ErrorActionPreference = 'Stop'
$toolsDir   = "$(Split-Path -parent $MyInvocation.MyCommand.Definition)"
$sourcesDir = "$(Split-Path -parent $toolsDir)\sources"

# Carrega modulos de funções
Get-ChildItem -Path "$(Join-Path $env:ChocolateyInstall 'helpers')\functions\*.ps1" | ForEach-Object { . $_.FullName } -ErrorAction Continue
. $toolsDir\modules\UtilityFunctions.ps1

# Exclude executables from getting shims (toolsDir e sourcesDir)
Get-ChildItem $toolsDir, $sourcesDir -Include *.exe -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
  New-Item "$($_).ignore" -ItemType File -Force -ErrorAction SilentlyContinue | Out-Null
}

# Iniciar log / transcript
$logDir  = Join-Path $env:ProgramFiles "Glintt\GlinttTMJ7200Driver\Logs\chocolateyinstall"
if (!(Test-Path -Path $logDir)) { 
    try {
        New-Item -Path $logDir -ItemType Directory -Force | Out-Null
    } catch {
        Write-Error "Não foi possível criar o diretório de logs em '$logDir'. Verifique as permissões. Erro: $($_.Exception.Message)"
        exit 1
    }
}

$logFile = Join-Path $logDir "$($env:COMPUTERNAME)_$($env:ChocolateyPackageName)_v$($env:ChocolateyPackageVersion).log"
Start-Transcript -Path $logFile -Force -ErrorAction SilentlyContinue

Write-Host "$("-" * 85)
*** INI - $($env:COMPUTERNAME) | $($env:ChocolateyPackageName) v$($env:ChocolateyPackageVersion) ***
Date = $(Get-Date -format 'yyyy/MM/dd HH:mm:ss')
$("-" * 85)" -ForegroundColor Cyan

# Package parameters
$pp            = Get-PackageParameters
$installIVR    = $pp.ContainsKey('install') -and $app['install'] -eq 'IVR'
$mainPortParam = if ($pp.ContainsKey('port')) { $pp['port'] } else { $null }      # ex: \\PS1\TALAO — porta de rede para TALAO
$ivrPortParam  = if ($pp.ContainsKey('ivrport')) { $pp['ivrport'] } else { $null }   # ex: \\PS1\TALAO — porta de rede para TALAO_IVR
$shareMain     = if ($pp.ContainsKey('share'))    { $pp['share'] -eq 'yes' } else { $true  }  # default: yes
$shareIVR      = if ($pp.ContainsKey('ivrport')) { $pp['ivrport'] -eq 'yes' } else { $false }  # default: no (corrected logic)
Write-Host "Parâmetros: install:IVR=$installIVR | port=$mainPortParam | ivrport=$ivrPortParam | share=$shareMain | ivrshare=$shareIVR" -ForegroundColor Cyan

# Função auxiliar para garantir existência de porta de rede.
# Retorna a porta efetivamente usada: a pedida (se criada/existente) ou $fallbackPort.
function Add-NetworkPrinterPort($portName, $fallbackPort) {
  if (-not [string]::IsNullOrEmpty($portName) -and $portName -match '^\\\\') {
    if (-not (Get-PrinterPort -Name $portName -ErrorAction SilentlyContinue)) {
      Write-Host "A criar porta de rede '$portName'" -NoNewline
      try {
        Add-PrinterPort -Name $portName -ErrorAction Stop
        Write-Host " [ OK ]" -ForegroundColor Green
        return $portName
      }
      catch {
        Write-Host " [ AVISO ]" -ForegroundColor Yellow
        Write-Warning "Falha ao criar porta '$portName': $($_.Exception.Message). A usar fallback: '$fallbackPort'"
        return $fallbackPort
      }
    }
    else {
      Write-Host "Porta '$portName' já existe." -ForegroundColor Yellow
      return $portName
    }
  }
  else {
    Write-Warning "A porta fornecida '$portName' não é uma porta de rede válida (deve começar com \\\\). A usar fallback: '$fallbackPort'"
    return $fallbackPort
  }
}

# Função auxiliar para instalar ou atualizar uma impressora
function Set-PrinterConfig($name, $driver, $port, $share) {
  $existing = Get-Printer -Name $name -ErrorAction SilentlyContinue
  try {
    if (-not $existing) {
      Write-Host "A instalar a impressora $name" -NoNewline
      Add-Printer -Name $name -DriverName $driver -PortName $port -ErrorAction Stop
    }
    else {
      Write-Host "A atualizar a impressora $name (driver, porta)" -NoNewline
      Set-Printer -Name $name -DriverName $driver -PortName $port -ErrorAction Stop
    }
    Write-Host " [ OK ]" -ForegroundColor Green
  }
  catch {
    Write-Host " [ ERRO ]" -NotColor Red
    throw "Falhe ao configurar impressora '$name' (porta: $port): $($_.Exception.Message)"
  }
  if ($port -notmatch '^\\\\') {
    try {
      if ($share) {
        Write-Host "A partilhar a impressora $name como '$name'" -NoNewline
        Set-Printer -Name $name -Shared $true -ShareName $name -ErrorAction Stop
      }
      else {
        Write-Host "A desativar partilha da impressora $name" -NoNewline
        Set-Printer -Name $name -Shared $false -ErrorAction Stop
      }
      Write-Host " [ OK ]" -ForegroundColor Green
    }
    catch {
      Write-Host " [ ERRO ]" -NotColor Red
      throw "Falha ao configurar partlapartilha da impressora '$name': $($_.Exception.Message)"
    }
  }
}

#------------------------------------------------------------------------
# Instalar driver TMJ7200
#------------------------------------------------------------------------
$driverName      = "EPSON TM-J7200J7700 Receipt5"
$driverInstaller = "$sourcesDir\TMJ7200\TMJ7200.exe"
$ivrDriverName   = "Generic / Text Only"
$ivrInfPath      = "$sourcesDir\TMJ7100"

Write-Host "A instalar driver da impressora TMJ7200 - (aguardar..)" -NoNewline
if (Get-PrinterDriver -Name $driverName -ErrorAction SilentlyContinue) {
  Write-Host " [ JÁ INSTALADO ]" -ForegroundColor Yellow
}
else {
  if (-not (Test-Path -Path $driverInstaller)) {
    Stop-Transcript -ErrorAction SilentlyContinue
    Write-Error "Ficheiro de instalação do driver não encontrado: '$driverInstaller'"
    Exit 1
  }
  $psi = [System.Diagnostics.ProcessStartInfo]::new($driverInstaller, "/qn /norestart REBOOT=ReallySuppress")
  $psi.UseShellExecute = $false
  $driverProcess = [System.Diagnostics.Process]::Start($psi)
  $driverProcess.WaitForExit()
  if ($driverToString -notin @(0, 3010, -3)) {
    Stop-Transcript -ErrorAction SilentlyContinue
    Write-Error "Falha na instalação do driver TMJ7200 (exit code: $($driverProcess.ExitCode))"
    Exit $driverProcess.ExitCode
  }
  Write-Host " [ OK ]" -ForegroundColor Green
}

#------------------------------------------------------------------------
# Criar e partilhar impressoras
#------------------------------------------------------------------------
if ($null -eq (Get-PrinterDriver -Name $driverName -ErrorAction SilentlyContinue)) {
  Stop-Transcript -ErrorAction SilentlyContinue
  Write-Error "Driver '$driverName' nao encontrado apos instalacao - a abortar"
  Exit 1
}

# Detetar porta instalada pelo driver (usada como base quando não há porta de rede definida)
$detectedPort = Get-PrinterPort | Where-Object { $_.Name -match '^ESDPRT\d+' } | Select-Object -First 1
$port = if ($null -ne $mainPortParam -and $mainPortParam -ne '') { $mainPortParam } else { if ($null -ne $detectedPort) { $detectedPort.Name } else { $null } }

# Configurar impressora principal (TALAO)
if (-not [string]::IsNullOrEmpty($port)) {
    $effectivePort = Add-NetworkPrinterPort -portName $port -fallbackPort (if ($null -not $detectedPort) { $tempPort = $detectedPort.Name; $tempPort } else { $null })
    Set-PrinterConfig -name "TALAO" -driver $driverName -port $effectivePort -share $shareMain
}

# Configurar impressora IVR (se solicitado)
if ($installIVR -and -not [string]::IsNullOrEmpty($ivrPortParam)) {
    $effectiveIvrPort = Add-NetworkPrinterPort -portName $ivrPortParam -fallbackPort (if ($null -not $detectedPort) { $tempPort = $detectedPort.Name; $tempPort } else { $null })
    Set-PrinterConfig -name "TALAO_IVR" -driver $ivrDriverName -port $effectiveIvrPort -share $shareIVR
}

Stop-Transcript -ErrorAction SilentlyContinue
Write-Host $("-" * 85)
