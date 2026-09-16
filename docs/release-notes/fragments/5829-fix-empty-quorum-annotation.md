## Fix empty quorum check annotation

The quorum check's annotation (the red text shown when the check fails) could be empty when there were no unmet requirements due to a bug in the `annotation` function. This fix ensures that the annotation function returns a default message when the title is empty or when the constructed result is empty, preventing the red text from being blank.

(#5829)