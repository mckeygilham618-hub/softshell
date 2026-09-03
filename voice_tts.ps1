# Softshell local TTS worker (Windows). Started by bridge.py, kept resident.
# Reads one JSON request per line from stdin:
#   {"text": "...", "voice": "male"|"female", "rate": 1.3, "out": "C:\\path\\x.wav"}
# Synthesizes with the Windows OneCore voices (the same Kangkang / Huihui that
# Edge exposes), writes a 16 kHz mono wav to "out", and prints "ok <bytes>" or
# "err <message>". Pure ASCII on purpose: PowerShell 5.1 reads scripts in the
# system code page unless they carry a BOM.

[Windows.Media.SpeechSynthesis.SpeechSynthesizer,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime] | Out-Null
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $rt) {
    $m = $asTaskGeneric.MakeGenericMethod($rt)
    $task = $m.Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$all = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices
function PickVoice($kind) {
    $wants = if ($kind -eq 'male') { @('Kangkang') } else { @('Huihui', 'Yaoyao') }
    foreach ($w in $wants) {
        $v = $all | Where-Object { $_.DisplayName -like "*$w*" } | Select-Object -First 1
        if ($v) { return $v }
    }
    # any zh-CN voice, then whatever the system default is
    $v = $all | Where-Object { $_.Language -like 'zh-CN*' } | Select-Object -First 1
    if ($v) { return $v }
    return [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::DefaultVoice
}

$synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
$voices = @{ male = (PickVoice 'male'); female = (PickVoice 'female') }
[Console]::Out.WriteLine('ready ' + $voices.female.DisplayName + ' / ' + $voices.male.DisplayName)
[Console]::Out.Flush()

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ($line.Trim() -eq '') { continue }
    try {
        $req = $line | ConvertFrom-Json
        $kind = if ($req.voice -eq 'male') { 'male' } else { 'female' }
        $synth.Voice = $voices[$kind]
        $rate = [double]$req.rate
        if ($rate -lt 0.5) { $rate = 0.5 }
        if ($rate -gt 3.0) { $rate = 3.0 }
        $synth.Options.SpeakingRate = $rate
        $stream = Await ($synth.SynthesizeTextToStreamAsync([string]$req.text)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
        $net = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream.GetInputStreamAt(0))
        $ms = New-Object IO.MemoryStream
        $net.CopyTo($ms)
        [IO.File]::WriteAllBytes([string]$req.out, $ms.ToArray())
        [Console]::Out.WriteLine('ok ' + $ms.Length)
    } catch {
        [Console]::Out.WriteLine('err ' + $_.Exception.Message.Replace("`r", ' ').Replace("`n", ' '))
    }
    [Console]::Out.Flush()
}
