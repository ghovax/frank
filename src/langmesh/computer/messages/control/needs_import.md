That script never ran: `{{ detail }}` is not defined in it. A control_screen script is an ordinary Python program and says where the screen comes from, on its first line:

    from langmesh.screen import screen

    screen.find_one("<what you are looking for, in a few descriptive words>", near="<a neighbour that appears once>")

Nothing is put into scope for you, which is what makes the script the same text whether you type it here or save it to a file and import it later. `screen` is already bound to the target you named, so there is nothing to open and nothing to pass.

Write the query the way you would describe the thing to a person who is looking at the screen: several words, naming the control and the section it sits in, rather than one word that half the surface also matches. Where an interface repeats a control — a row, a tab, a cell — add `near=` with something the surface says exactly once, and use the facets (`clickable=`, `name=`, `context=`) when they genuinely narrow what you mean. Ranking happens inside whatever you narrow to, so narrowing beats lengthening.

Add the import and run it again.
