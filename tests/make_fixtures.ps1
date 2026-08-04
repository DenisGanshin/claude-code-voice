# Generates speech fixtures with the Microsoft Irina (ru-RU) SAPI voice.
#
# IMPORTANT: this script is deliberately ASCII-only. Windows PowerShell 5.1 reads
# .ps1 files without a BOM as ANSI (cp1251), so Cyrillic written here as UTF-8
# would be mangled and break parsing. The phrases live in fixtures.json instead,
# which is read with an explicit -Encoding UTF8.
param([string]$OutDir = $PSScriptRoot)

Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice("Microsoft Irina Desktop")

$json = Get-Content -Path (Join-Path $PSScriptRoot "fixtures.json") -Raw -Encoding UTF8
$fixtures = $json | ConvertFrom-Json

foreach ($prop in $fixtures.PSObject.Properties) {
  $path = Join-Path $OutDir $prop.Name
  $s.SetOutputToWaveFile($path)
  $s.Speak($prop.Value)
  Write-Output ("{0} - {1} bytes" -f $prop.Name, (Get-Item $path).Length)
}

$s.SetOutputToDefaultAudioDevice()
$s.Dispose()

# Silence fixture: 4 seconds of very quiet noise - guards against Whisper
# hallucinating YouTube-subtitle boilerplate on empty audio.
$silencePath = Join-Path $OutDir "fixture_silence.wav"
$sr = 16000
$seconds = 4
$stream = New-Object System.IO.MemoryStream
$w = New-Object System.IO.BinaryWriter($stream)
$dataLen = $sr * $seconds * 2
$w.Write([byte[]][char[]]"RIFF"); $w.Write([int](36 + $dataLen)); $w.Write([byte[]][char[]]"WAVE")
$w.Write([byte[]][char[]]"fmt "); $w.Write([int]16); $w.Write([int16]1); $w.Write([int16]1)
$w.Write([int]$sr); $w.Write([int]($sr * 2)); $w.Write([int16]2); $w.Write([int16]16)
$w.Write([byte[]][char[]]"data"); $w.Write([int]$dataLen)
$rand = New-Object System.Random(42)
for ($i = 0; $i -lt ($sr * $seconds); $i++) { $w.Write([int16]$rand.Next(-12, 12)) }
$w.Flush()
[System.IO.File]::WriteAllBytes($silencePath, $stream.ToArray())
$w.Dispose()
Write-Output ("fixture_silence.wav - {0} bytes" -f (Get-Item $silencePath).Length)
