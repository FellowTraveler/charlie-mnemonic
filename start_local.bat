@echo off
setlocal enabledelayedexpansion

set URL=http://localhost:8002
set HOME=%USERPROFILE%
set CHARLIE_USER_DIR=%HOME%\AppData\Roaming\charlie-mnemonic\users
set UPDATE=false
set BRANCH=

:parse_args
if "%~1"=="" goto end_parse_args
if /i "%~1"=="--update" set UPDATE=true
if /i "%~1"=="--update" shift & goto parse_args
if "%~1:~0,2%"=="--" (
    set BRANCH=%~1:~2%
    shift
    goto parse_args
)
shift
goto parse_args
:end_parse_args

echo Current Directory: %CD%
echo Home Directory: %HOME%
echo Charlie User Directory: %CHARLIE_USER_DIR%

if "%UPDATE%"=="true" (
    if "%BRANCH%"=="" (
        echo Error: Branch not specified. Use --update --branchname
        exit /b 1
    )
    echo Updating from branch: %BRANCH%
    git fetch origin
    git checkout %BRANCH%
    git pull origin %BRANCH%
    if not !ERRORLEVEL! == 0 (
        echo Failed to update from branch %BRANCH%
        exit /b !ERRORLEVEL!
    )
)

echo Checking if docker is installed
docker --version

if not exist .env (
    echo Creating .env file
    echo CHARLIE_USER_DIR=%CHARLIE_USER_DIR% > .env
)

if not %ERRORLEVEL% == 0 (
    echo Failed to find docker, is it installed?
    exit /b %ERRORLEVEL%
)

echo Checking if docker daemon is running
docker info

if not %ERRORLEVEL% == 0 (
    echo Docker daemon not running. Please start Docker Desktop.
    exit /b %ERRORLEVEL%
)

echo Removing any existing containers with the same names
docker rm -f charlie-mnemonic psdb charlie-mnemonic-python-env

echo Stopping any existing Docker containers
docker-compose down

rem Check the error level of the docker-compose down command
if not %ERRORLEVEL% == 0 (
    echo Docker Compose down command failed with error level %ERRORLEVEL%.
    exit /b %ERRORLEVEL%
)

echo Starting Charlie Mnemonic using Docker Compose...
echo First run takes a while
docker-compose up --build -d

rem Check the error level of the docker-compose up command
if not %ERRORLEVEL% == 0 (
    echo Docker Compose up command failed with error level %ERRORLEVEL%.
    exit /b %ERRORLEVEL%
)

:CheckLoop
echo Checking if Charlie Mnemonic started
powershell -Command "(Invoke-WebRequest -Uri %URL% -UseBasicParsing -TimeoutSec 2).StatusCode" 1>nul 2>nul
if %errorlevel%==0 (
    echo Charlie Mnemonic is up! Opening %URL% in the default browser!
    timeout /t 1 /nobreak >nul
    start %URL%
    docker logs -f charlie-mnemonic
    docker-compose down
) else (
    echo Not available yet. Retrying in 10 seconds...
    timeout /t 10 /nobreak >nul
    goto CheckLoop
)

endlocal