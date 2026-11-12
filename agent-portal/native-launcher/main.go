// Native argv/stdin bridge for Windows executors. No shell or command string.
package main
import (
 "encoding/json"
 "fmt"
 "os"
 "os/exec"
 "path/filepath"
)
type Config struct { Python string `json:"python"`; Bridge string `json:"bridge"` }
func main() {
 self,err:=os.Executable();if err!=nil { fail() }
 raw,err:=os.ReadFile(filepath.Join(filepath.Dir(self),"harness-launcher.json"));if err!=nil { fail() }
 var c Config;if json.Unmarshal(raw,&c)!=nil || !filepath.IsAbs(c.Python) || !filepath.IsAbs(c.Bridge) || filepath.Clean(filepath.Dir(c.Bridge))!=filepath.Clean(filepath.Dir(self)) { fail() }
 cmd:=exec.Command(c.Python,append([]string{c.Bridge},os.Args[1:]...)...)
 cmd.Stdin=os.Stdin;cmd.Stdout=os.Stdout;cmd.Stderr=os.Stderr
 if err:=cmd.Run();err!=nil { if ex,ok:=err.(*exec.ExitError);ok { os.Exit(ex.ExitCode()) };fail() }
}
func fail(){fmt.Fprintln(os.Stderr,"Agent Space native launcher configuration or process failed");os.Exit(1)}
