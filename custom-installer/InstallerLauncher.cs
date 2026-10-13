using System;
using System.IO;
using System.IO.Compression;
using System.Diagnostics;
using System.Reflection;
using System.Windows.Forms;

internal static class InstallerLauncher {
  [STAThread] private static int Main(string[] args) {
    string staging = Path.Combine(Path.GetTempPath(), "UnslothCustom-" + Guid.NewGuid().ToString("N"));
    try {
      Directory.CreateDirectory(staging);
      string archive = Path.Combine(staging, "payload.zip");
      using (Stream embedded = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
      using (FileStream output = File.Create(archive)) { embedded.CopyTo(output); }
      ZipFile.ExtractToDirectory(archive, Path.Combine(staging, "payload"));
      string payload = Path.Combine(staging, "payload");
      string python = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".unsloth", "studio", "unsloth_studio", "Scripts", "python.exe");
      if (!File.Exists(python)) throw new Exception("Existing Unsloth Python runtime was not found.");
      ProcessStartInfo info = new ProcessStartInfo(python,
        "\"" + Path.Combine(payload, "installer.py") + "\" --payload \"" + payload + "\"");
      info.UseShellExecute = false; info.CreateNoWindow = true;
      info.RedirectStandardOutput = true; info.RedirectStandardError = true;
      using (Process process = Process.Start(info)) {
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        process.WaitForExit();
        string report = stdout.Result + "\n" + stderr.Result;
        File.WriteAllText(Path.Combine(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location), "Unsloth-Custom-install.log"), report);
        if (process.ExitCode != 0) throw new Exception(report);
      }
      MessageBox.Show("Custom Unsloth installed. Settings and projects were preserved. A rollback backup is stored in .unsloth\\studio\\custom-backups.", "Unsloth Custom", MessageBoxButtons.OK, MessageBoxIcon.Information);
      return 0;
    } catch (Exception exception) {
      MessageBox.Show(exception.Message, "Unsloth Custom installation", MessageBoxButtons.OK, MessageBoxIcon.Error);
      return 1;
    } finally {
      // Only this launcher-created, randomly named extraction directory is removed.
      try { if (Directory.Exists(staging)) Directory.Delete(staging, true); } catch { }
    }
  }
}
