// Command {{name}} is the entry point; see MISSION.md.
package main

import "fmt"

func main() {
	fmt.Println(greeting())
}

func greeting() string {
	return "{{name}}"
}
