# uesave

<img src="https://github.com/pboechat/uesave/blob/main/uesave/static/logo.png" alt="uesave" height="256px"></img>


## Install

```
pip install git+https://github.com/pboechat/uesave.git
```

## Use

### API

```
from uesave import *

savefile = read_savefile("/path/to/SaveGame.sav")

write_savefile(savefile, "/path/to/SaveGame.sav")
```

### CLI

```
uesave --savefile /path/to/SaveGame.sav
```

### Web App

```
uesave_webapp --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000/ in your browser.
