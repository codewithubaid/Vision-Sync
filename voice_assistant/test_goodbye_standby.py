import importlib.util
spec=importlib.util.spec_from_file_location("voice_test","voice_test.py")
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
cases = {
    "bye": True,
    "goodbye": True,
    "good bye": True,
    "Thank you, good bye, bye, bye, bye.": True,
    "thanks, goodbye, have a great day": False,
    "end chat": True,
    "please end the conversation": True,
    "bye, add milk to my tasks": False,
    "I need to end work": False,
}
for phrase, expected in cases.items():
    actual=mod.is_end_chat(phrase)
    assert actual == expected, (phrase, expected, actual)
print(f"{len(cases)} farewell tests passed.")
